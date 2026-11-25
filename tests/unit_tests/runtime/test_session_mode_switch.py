"""Explicit mode switches from Web keep one Session and its project index."""

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

from agent_gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime, SessionModeSwitchError
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata


@pytest.fixture(params=sorted(session_metadata.MODE_SWITCH_CHANNELS))
def session_channel(request):
    """Every mode-switch channel must run the same mode.set and mode.get logic."""
    return request.param


@pytest.fixture(params=[("default_code", ""), ("project-123", "C:/projects/sample")])
def mode_session(session_channel, tmp_path, monkeypatch, request):
    sessions = tmp_path / "sessions"
    project_id, project_dir = request.param
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    session_metadata.init_session_metadata(
        session_id="one-session",
        channel_id=session_channel,
        user_id="user-1",
        mode="agent.code.normal",
        project_id=project_id,
        project_dir=project_dir,
        work_mode="code",
    )
    yield project_id, project_dir
    session_metadata._METADATA_QUEUE.join()
    session_metadata.remove_session_metadata_cache("one-session")


def _switch(runtime, channel_id, target_mode, expected_mode, *, user_id="user-1"):
    """Call the public mode.set entry for the fixture Session."""
    return runtime.switch_session_mode(
        session_id="one-session",
        target_mode=target_mode,
        expected_mode=expected_mode,
        channel_id=channel_id,
        user_id=user_id,
    )


def _get_mode(channel_id, *, user_id="user-1"):
    """Call the public mode.get entry for the fixture Session."""
    return AgentRuntime.get_session_mode(
        session_id="one-session", channel_id=channel_id, user_id=user_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,blocked", [
    ("observer", False),
    ("observer_without_stream", False),
    ("finished_turn", False),
    ("pending_turn", True),
    ("inflight_turn", True),
    ("active_round", True),
    ("removed_observer", True),
])
async def test_mode_switch_with_team_observer(
    mode_session, session_channel, monkeypatch, scenario, blocked,
):
    from jiuwenswarm.agents.harness.team import team_manager

    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    stream = asyncio.get_running_loop().create_future()
    if scenario != "observer_without_stream":
        manager._stream_tasks["one-session"] = stream
    manager.add_waiter("one-session", "observer", asyncio.Queue(), attach_only=True)
    runtime.begin_chat_request("one-session", "observer")
    if scenario == "finished_turn":
        runtime.begin_chat_request("one-session", "turn")
        manager.begin_round("one-session", "turn")
        assert await manager.release_round("one-session", "turn")
    elif scenario == "pending_turn":
        runtime.begin_chat_request("one-session", "turn")
    elif scenario == "inflight_turn":
        manager.begin_request("one-session", "automation")
    elif scenario == "active_round":
        manager.begin_round("one-session", "automation")
    elif scenario == "removed_observer":
        manager.remove_waiter("one-session", "observer")

    try:
        if blocked:
            with pytest.raises(SessionModeSwitchError) as busy:
                _switch(runtime, session_channel, "agent.work.normal", "team.work.normal")
            assert busy.value.code == "SESSION_BUSY"
            assert _get_mode(session_channel)["mode"] == "team.work.normal"
        else:
            result = _switch(runtime, session_channel, "agent.work.normal", "team.work.normal")
            assert result["changed"] is True
            assert _get_mode(session_channel)["mode"] == "agent.work.normal"
    finally:
        stream.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("team_work,tracked,blocked", [
    (None, True, False),
    ("round", True, True),
    ("round", False, True),
    ("request", False, True),
])
async def test_mode_switch_with_observer_attached_after_the_round(
    mode_session, session_channel, monkeypatch, team_work, tracked, blocked,
):
    """A page reloaded after a Team round re-attaches while the leader stream lives on.

    Nothing parks that observer until the next round, so its execution stays
    RUNNING. It owns no turn and must not refuse the switch; Team work does.
    """
    from contextlib import AsyncExitStack, aclosing

    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.runtime.session import RuntimeSessionCoordinator
    from jiuwenswarm.runtime.session.model import (
        TEAM_ATTACH_ONLY_PARAM,
        SessionExecutionState,
        TeamExecutionDetails,
        TeamWaiterKind,
    )
    from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _consume_attach_only_stream

    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    coordinator = RuntimeSessionCoordinator()
    session = await coordinator.register_session("one-session", session_channel)
    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    manager.bind_generation("one-session", session.generation)
    runtime = AgentRuntime(
        agent_manager=AsyncMock(),
        initializer=AsyncMock(),
        session_coordinator=coordinator,
        session_runtime_lifecycle=TeamSessionLifecycleAdapter(coordinator=coordinator, team_manager=manager),
    )
    _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
    # The round is over; the persistent leader stream is still registered.
    leader_stream = asyncio.get_running_loop().create_future()
    manager._stream_tasks["one-session"] = leader_stream
    attached = asyncio.Event()
    startup = AsyncExitStack()

    async def signal_attached() -> None:
        attached.set()

    startup.push_async_callback(signal_attached)
    request = AgentRequest(
        request_id="observer",
        channel_id=session_channel,
        session_id="one-session",
        params={"mode": "team", TEAM_ATTACH_ONLY_PARAM: True},
    )

    async def observe() -> None:
        events = coordinator.run_team_stream(
            "one-session",
            "observer",
            SessionWorkKind.TEAM_REQUEST,
            lambda context: _consume_attach_only_stream(
                request=request,
                team_manager=manager,
                startup=startup,
                generation=context.session.generation,
            ),
            details=TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT, observation_only=True),
        )
        async with aclosing(events):
            async for _chunk in events:
                pass

    observer = asyncio.create_task(observe())
    if tracked:
        runtime.begin_chat_request("one-session", "observer")
    try:
        await asyncio.wait_for(attached.wait(), timeout=2)
        execution = next(
            item for item in coordinator.snapshot_session("one-session").executions
            if item.request_id == "observer"
        )
        assert execution.state is SessionExecutionState.RUNNING
        if team_work == "round":
            manager.begin_round("one-session", "automation")
        elif team_work == "request":
            manager.begin_request("one-session", "automation")

        if blocked:
            with pytest.raises(SessionModeSwitchError) as busy:
                _switch(runtime, session_channel, "agent.work.normal", "team.work.normal")
            assert busy.value.code == "SESSION_BUSY"
            assert _get_mode(session_channel)["mode"] == "team.work.normal"
        else:
            result = _switch(runtime, session_channel, "agent.work.normal", "team.work.normal")
            assert result["changed"] is True
            assert _get_mode(session_channel)["mode"] == "agent.work.normal"
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        leader_stream.cancel()
        await coordinator.close_session("one-session")


def test_mode_set_preserves_project_index_and_blocks_stale_write(mode_session, session_channel, monkeypatch):
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )

    project_id, project_dir = mode_session
    assert _switch(runtime, session_channel, "agent.work.normal", "agent.code.normal") == {
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

    assert _switch(runtime, session_channel, "agent.work.normal", "agent.code.normal")["changed"] is False
    assert len(markers) == 1


def test_mode_set_rejects_stale_expectation_busy_and_foreign_user(mode_session, session_channel):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as conflict:
        _switch(runtime, session_channel, "team.work.normal", "agent.work.normal")
    assert conflict.value.code == "MODE_CONFLICT"

    with pytest.raises(SessionModeSwitchError) as denied:
        _switch(runtime, session_channel, "agent.work.normal", "agent.code.normal", user_id="user-2")
    assert denied.value.code == "NOT_FOUND"

    runtime.begin_chat_request("one-session", "in-flight")
    with pytest.raises(SessionModeSwitchError) as busy:
        _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
    assert busy.value.code == "SESSION_BUSY"
    runtime.end_chat_request("one-session", "in-flight")

    assert session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )["mode"] == "agent.code.normal"


def test_mode_get_reads_persisted_mode_and_checks_user(mode_session, session_channel):
    project_id, _project_dir = mode_session
    assert _get_mode(session_channel, user_id="user-1") == {
        "mode": "agent.code.normal",
        "work_mode": "code",
        "project_id": project_id,
    }
    with pytest.raises(SessionModeSwitchError) as denied:
        _get_mode(session_channel, user_id="user-2")
    assert denied.value.code == "NOT_FOUND"


def test_mode_set_is_refused_in_the_enterprise_edition(mode_session, session_channel, monkeypatch):
    """The enterprise edition fixes a conversation's mode on the server too; mode.get keeps answering."""
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    for target in ("team.code.normal", "agent.code.normal"):
        with pytest.raises(SessionModeSwitchError) as refused:
            _switch(runtime, session_channel, target, "agent.code.normal")
        assert refused.value.code == "BAD_REQUEST"
        assert "enterprise" in str(refused.value)
    assert _get_mode(session_channel)["mode"] == "agent.code.normal"
    assert markers == []
    assert "mode_revision" not in session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    assert _switch(runtime, session_channel, "team.code.normal", "agent.code.normal")["changed"] is True
    assert len(markers) == 1


@pytest.mark.parametrize("mode_fields", [
    {}, {"mode": None}, {"mode": "unknown"}, {"mode": ""},
    {"mode": "future.mode"}, {"mode": 1}, {"mode": []}, {"mode": {}},
    {"mode": "team.design"},
])
def test_unknown_mode_requires_explicit_repair(mode_session, session_channel, monkeypatch, mode_fields):
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored.pop("mode", None)
    stored.update(mode_fields)
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(SessionModeSwitchError) as unknown:
        _get_mode(session_channel, user_id="user-1")
    assert unknown.value.code == "MODE_UNKNOWN"
    assert "explicit mode" in str(unknown.value)
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored

    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as denied:
        _switch(runtime, session_channel, "team.work.normal", "agent.code.normal", user_id="user-2")
    assert denied.value.code == "NOT_FOUND"

    runtime.begin_chat_request("one-session", "in-flight")
    with pytest.raises(SessionModeSwitchError) as busy:
        _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
    assert busy.value.code == "SESSION_BUSY"
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored
    runtime.end_chat_request("one-session", "in-flight")

    result = _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
    assert result["mode"] == "team.work.normal"
    assert result["previous_mode"] is None
    assert result["changed"] is True
    persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert persisted["mode"] == "team.work.normal"
    assert persisted["mode_revision"] == 1
    for field in ("project_id", "project_dir", "work_mode"):
        assert persisted[field] == stored[field]
    assert _get_mode(session_channel, user_id="user-1")["mode"] == result["mode"]
    assert len(markers) == 1
    assert markers[0]["previous_mode"] is None

    # A later client cannot use the unknown-route repair to overwrite a known route.
    with pytest.raises(SessionModeSwitchError) as conflict:
        _switch(runtime, session_channel, "agent.work.normal", "agent.code.normal")
    assert conflict.value.code == "MODE_CONFLICT"


@pytest.mark.parametrize("stored, client", [
    ("agent.code.plan", "agent.code.normal"),
    ("agent.code.normal", "agent.code.plan"),
])
def test_mode_set_accepts_plan_drift_and_keeps_queued_fields(
    mode_session, session_channel, monkeypatch, stored, client,
):
    """Plan state bypasses mode.set, and a commit drops only the queued route."""
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    session_metadata.patch_session_metadata("one-session", {"mode": stored})
    queued = {"title": (10**9, "queued"), "mode": (10**9, stored), "mode_revision": (10**9, 0)}
    with session_metadata._CACHE_LOCK:
        session_metadata._PENDING_FIELDS[session_metadata._metadata_cache_key("one-session")] = queued
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())

    result = _switch(runtime, session_channel, "team.code.normal", client)
    assert (result["previous_mode"], result["mode"], result["changed"]) == (stored, "team.code.normal", True)
    cached = session_metadata.get_session_metadata("one-session", enable_writeback=False)
    assert (cached["title"], cached["mode"], cached["mode_revision"]) == ("queued", "team.code.normal", 1)
    with pytest.raises(SessionModeSwitchError) as conflict:
        _switch(runtime, session_channel, "agent.code.normal", "agent.work.plan")
    assert conflict.value.code == "MODE_CONFLICT"


@pytest.mark.parametrize("legacy, canonical", [
    ("team", "team.work.normal"), ("code.team", "team.code.normal"),
    ("code.normal", "agent.code.normal"), ("agent.plan", "agent.work.plan"),
])
def test_legacy_idempotent_mode_set_returns_canonical(mode_session, session_channel, monkeypatch, legacy, canonical):
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    session_metadata.patch_session_metadata("one-session", {"mode": legacy})
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    result = _switch(runtime, session_channel, canonical, canonical)
    assert result["mode"] == canonical
    assert result["previous_mode"] == canonical
    assert result["changed"] is False
    assert _get_mode(session_channel, user_id="user-1")["mode"] == canonical
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["mode"] == legacy
    assert markers == []


@pytest.mark.parametrize("stored_mode", ["agent.code.normal", "unknown"])
def test_mode_set_rejects_foreign_live_runtime_owner(mode_session, session_channel, stored_mode):
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
            _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
        assert busy.value.code == "SESSION_BUSY"
        assert session_metadata.get_session_metadata(
            "one-session", cache_bust=True, enable_writeback=False,
        )["mode"] == stored_mode
    finally:
        owner.terminate()
        owner.wait(timeout=5)


def test_uncommitted_cross_profile_chat_uses_committed_mode(mode_session, session_channel, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _switch(runtime, session_channel, "agent.work.normal", "agent.code.normal")
    request = AgentRequest(
        request_id="late-chat",
        channel_id=session_channel,
        session_id="one-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "work_mode": "code", "query": "hello"},
    )
    normalized = AgentRuntime.normalize_request(request)
    assert normalized.params["mode"] == "agent.work.normal"
    assert normalized.params["work_mode"] == "code"


@pytest.mark.asyncio
async def test_cold_chat_reclassifies_after_remote_switch_before_claim(mode_session, session_channel, monkeypatch):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    request = AgentRequest(
        request_id="cold-chat",
        channel_id=session_channel,
        session_id="one-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "query": "hello"},
    )
    assert AgentRuntime.session_work_kind(AgentRuntime.normalize_request(request)) is SessionWorkKind.CHAT_UNARY

    async def claim_after_remote_commit(_request):
        session_metadata.compare_and_set_session_mode(
            "one-session", "team.work.normal", "agent.code.normal",
            channel_id=session_channel, user_id="user-1",
        )
        lifecycle.claim_runtime("one-session")

    monkeypatch.setattr(runtime, "_ensure_session_registered", claim_after_remote_commit)
    normalized = await runtime._normalize_chat_after_claim(request)
    assert normalized.params["mode"] == "team.work.normal"
    assert AgentRuntime.session_work_kind(normalized) is SessionWorkKind.TEAM_REQUEST


@pytest.mark.asyncio
async def test_adopted_chat_reads_authoritative_metadata_once(mode_session, session_channel, monkeypatch):
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
        request_id="warm-chat", channel_id=session_channel, session_id="one-session",
        req_method=ReqMethod.CHAT_SEND, params={"mode": "agent.code.normal", "query": "hello"},
    )
    normalized = await runtime._normalize_chat_after_claim(request)
    assert normalized.params["mode"] == "agent.code.normal"
    assert len(reads) == 1
    assert reads[0]["cache_bust"] is True
    registered.assert_awaited_once_with(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_mode, params, work_kind", [
    # Upstream classifies a plan chat as single-Agent chat work; the early claim must leave that as it is.
    ("agent.code.normal", {"mode": "agent.code.plan", "query": "plan this"}, SessionWorkKind.CHAT_UNARY),
    ("agent.code.normal", {"mode": "auto_harness", "query": "run"}, None),
    ("agent.code.normal", {"mode": "agent.code.normal", "query": "hello"}, SessionWorkKind.CHAT_UNARY),
    ("team.code.normal", {"mode": "team.code.normal", "query": ""}, SessionWorkKind.TEAM_REQUEST),
], ids=["plan-chat", "auto-harness-chat", "agent-chat", "team-attach"])
async def test_chat_on_never_switched_session_keeps_its_route(
    mode_session, session_channel, monkeypatch, stored_mode, params, work_kind,
):
    """Without a committed mode.set (switch off), the early claim changes no chat's route or work kind."""
    from jiuwenswarm.runtime.session.model import TEAM_ATTACH_ONLY_PARAM

    session_metadata.patch_session_metadata("one-session", {"mode": stored_mode})
    assert "mode_revision" not in session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    if work_kind is SessionWorkKind.TEAM_REQUEST:
        # Opening a Team conversation in the Web UI subscribes with attach_only.
        params = {**params, TEAM_ATTACH_ONLY_PARAM: True}
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    monkeypatch.setattr(runtime, "_ensure_session_registered", AsyncMock())
    request = AgentRequest(
        request_id="plain-chat", channel_id=session_channel, session_id="one-session",
        req_method=ReqMethod.CHAT_SEND, params=dict(params),
    )
    normalized = await runtime._normalize_chat_after_claim(request)
    assert normalized.params["mode"] == params["mode"]
    assert AgentRuntime.session_work_kind(normalized) is work_kind
    assert runtime._non_team_attachment_completion(normalized) is None


@pytest.mark.parametrize("method", ["mode.set", "mode.get"])
def test_mode_requests_are_forwarded_by_the_web_channel(method):
    """The Gateway must hand Web mode requests to AgentServer."""
    assert ReqMethod(method).value == method
    assert method in app_web_handlers._FORWARD_REQ_METHODS
    assert method in app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS


@pytest.mark.parametrize("stored, caller, owns", [
    ("web", "web", True),
    ("web", "feishu", False),
    ("", "web", False),
    (None, "web", False),
    ("process_cli", "process_cli", False),
    ("feishu", "web", False),
])
def test_mode_switch_channel_owns_only_its_own_sessions(stored, caller, owns):
    """Web addresses only Sessions it created; other channels and unchanneled Sessions are refused."""
    assert session_metadata.mode_switch_channel_owns({"channel_id": stored}, caller) is owns


@pytest.mark.parametrize("stored_channel", ["feishu", ""])
def test_mode_switch_rejects_a_session_owned_by_another_channel(
    mode_session, session_channel, monkeypatch, stored_channel,
):
    """A Session another channel created, or one without a recorded channel, cannot be switched."""
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored["channel_id"] = stored_channel
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())

    with pytest.raises(SessionModeSwitchError) as switch:
        _switch(runtime, session_channel, "team.work.normal", "agent.code.normal")
    assert switch.value.code == "NOT_FOUND"
    with pytest.raises(SessionModeSwitchError) as read:
        _get_mode(session_channel)
    assert read.value.code == "NOT_FOUND"
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored
    assert markers == []


_NPC_ROSTER = {"members": [{"name": "fisherman", "role_instructions": "You are the fisherman.", "title": "", "desc": "fish"}]}


@pytest.mark.parametrize("bound_fields", [
    {"agent_group_name": "research-experts"},
    {"team_group_identity": {"name": "research-experts"}},
    {"session_runtime_binding": {"agent": "claude"}},
    {"cron_id": "daily-report"},
    {"execution_profile": "app_role", "role_instructions": "You are the village chief."},
    {"execution_profile": "app_role", "role_instructions": "You are the village chief.",
     "npc_team": _NPC_ROSTER, "npc_member_sessions": {"fisherman": "member-session"}},
])
def test_mode_set_rejects_sessions_with_a_bound_executor(mode_session, session_channel, monkeypatch, bound_fields):
    """Agent groups, external agents, scheduled tasks and application roles keep their executor for life."""
    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored.update(bound_fields)
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())

    with pytest.raises(SessionModeSwitchError) as rejected:
        _switch(runtime, session_channel, "team.code.normal", "agent.code.normal")
    assert rejected.value.code == "BAD_REQUEST"
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored
    assert markers == []
    # Reading the route stays available so a client can show why it is fixed.
    assert _get_mode(session_channel)["mode"] == "agent.code.normal"


def test_mode_set_cannot_move_a_native_npc_team_out_of_team_mode(mode_session, session_channel, monkeypatch):
    """An application role on the native Team runtime keeps the Team mode it was created with."""
    from jiuwenswarm.runtime.app_role import is_app_role_team

    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored.update({"execution_profile": "app_role", "role_instructions": "You are the village chief.",
                   "npc_team": _NPC_ROSTER, "mode": "team.code.normal"})
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")
    assert is_app_role_team(stored)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())

    with pytest.raises(SessionModeSwitchError) as rejected:
        _switch(runtime, session_channel, "agent.code.normal", "team.code.normal")
    assert rejected.value.code == "BAD_REQUEST"
    assert "application role" in str(rejected.value)
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == stored
    assert markers == []
    assert _get_mode(session_channel)["mode"] == "team.code.normal"


@pytest.mark.asyncio
async def test_mode_get_waits_for_timed_out_mode_sets_during_runtime_start(monkeypatch, session_channel):
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

    def get_session_mode(*, session_id, channel_id, user_id):
        assert (session_id, channel_id, user_id) == ("one-session", session_channel, "user-1")
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
            channel_id=session_channel,
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


def _commit_and_flush(runtime, channel_id, target_mode="agent.work.normal"):
    _switch(runtime, channel_id, target_mode, "agent.code.normal")
    session_metadata.flush_pending_writes()


def _persisted_mode():
    metadata = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    return metadata["mode"], metadata.get("mode_revision")


def test_late_team_record_keeps_label_but_not_routing_mode(mode_session, session_channel, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    items = _capture_transcript_items(monkeypatch, session_history)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _commit_and_flush(runtime, session_channel)
    assert _persisted_mode() == ("agent.work.normal", 1)

    # A team monitor frame flushed after the switch: ``mode`` here is the
    # transcript label (``_is_team_relevant`` reads it), never a routing
    # decision. Writing it back would resurrect the route mode.set committed.
    session_history.append_history_record(
        session_id="one-session",
        request_id="team-member-1",
        channel_id=session_channel,
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


def test_late_subagent_record_cannot_restore_stale_routing_mode(mode_session, session_channel, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    items = _capture_transcript_items(monkeypatch, session_history)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _commit_and_flush(runtime, session_channel)
    assert _persisted_mode() == ("agent.work.normal", 1)

    # agent_observability forwards a sub-agent LLM record into the parent
    # Session without ``subagent_id``, carrying the mode captured when the
    # parent request started — already stale after the switch above.
    session_history.append_history_record(
        session_id="one-session",
        request_id="llm-end-1",
        channel_id=session_channel,
        role="assistant",
        content="sub-agent llm record",
        timestamp=1_700_000_001.0,
        event_type="llm.end",
        mode="agent.code.normal",
    )
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)
    assert items[-1]["mode"] == "agent.code.normal"


def test_child_transcript_record_still_ignores_its_subagent_label(mode_session, session_channel, monkeypatch):
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


def test_single_read_mode_migration_cannot_undo_a_committed_switch(mode_session, session_channel, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = _legacy_snapshot()  # read before the switch: legacy "team.plan"
    migrated = dict(stale, mode="team.work.plan")  # what the migration computes

    _commit_and_flush(runtime, session_channel)  # mode.set lands between read and write-back
    assert _persisted_mode() == ("agent.work.normal", 1)

    # Single-read write-back path: no expected_fields, only the revision guard.
    session_metadata._enqueue_write(
        "one-session", migrated, preserve_pin_fields=True,
        merge_fields=frozenset({"mode"}),
    )
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)


def test_batch_mode_migration_skips_a_route_changed_after_the_read(mode_session, session_channel, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = _legacy_snapshot()
    # Equal revision on purpose: only the field-level CAS can reject this write.
    migrated = dict(stale, mode="team.work.plan", mode_revision=1)

    _commit_and_flush(runtime, session_channel)
    assert _persisted_mode() == ("agent.work.normal", 1)

    session_metadata._enqueue_inference_write("one-session", migrated, {"mode"}, stale)
    session_metadata.flush_pending_writes()

    assert _persisted_mode() == ("agent.work.normal", 1)


def _sync_request_mode(channel_id, mode):
    """Write a request's explicit mode the way chat metadata sync does."""
    session_metadata.sync_session_request_metadata(
        session_id="one-session", channel_id=channel_id, mode=mode,
        explicit_mode_provided=True, user_id="user-1",
    )
    session_metadata.flush_pending_writes()


@pytest.mark.parametrize("requested, stored", [
    ("agent.code.normal", "team.code.normal"),
    ("agent.work.normal", "team.code.normal"),
    ("team.work.normal", "team.code.normal"),
    # Plan entry and exit do not go through mode.set, so plan state may drift.
    ("team.code.plan", "team.code.plan"),
])
def test_stale_request_mode_cannot_undo_a_committed_switch(
    mode_session, session_channel, monkeypatch, requested, stored,
):
    """A request that reaches metadata sync without Runtime normalization keeps the committed route."""
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    _switch(runtime, session_channel, "team.code.normal", "agent.code.normal")

    _sync_request_mode(session_channel, requested)

    assert _persisted_mode() == (stored, 1)


def test_request_mode_still_sets_a_never_switched_session(mode_session, session_channel):
    """Without a committed mode.set the explicit request mode is stored as on develop."""
    _sync_request_mode(session_channel, "team.code.normal")

    assert _persisted_mode() == ("team.code.normal", None)


def test_team_history_filter_still_reads_the_record_mode():
    relevant = session_history._is_team_relevant
    assert relevant({"event_type": "chat.tool_call", "mode": "team"}) is True
    assert relevant({"event_type": "chat.tool_call", "mode": "team.code.normal"}) is True
    assert relevant({"event_type": "chat.tool_call", "mode": "agent.work.normal"}) is False

@pytest.mark.asyncio
@pytest.mark.parametrize("method,channel_id,params", [
    (ReqMethod.MODE_SET, "process_cli", {"session_id": "one-session", "mode": "team.work.normal"}),
    (ReqMethod.MODE_GET, "process_cli", {"session_id": "one-session"}),
    (ReqMethod.MODE_SET, "feishu", {"session_id": "one-session", "mode": "team.work.normal"}),
    (ReqMethod.MODE_SET, "web", {
        "session_id": "one-session", "mode": "agent.work.normal", "expected_mode": 3,
    }),
    (ReqMethod.MODE_SET, "web", {"session_id": "other-session", "mode": "agent.work.normal"}),
    (ReqMethod.MODE_SET, "web", {"session_id": "one-session"}),
    # A switch must name the mode it expects to replace; a blind overwrite is refused.
    (ReqMethod.MODE_SET, "web", {"session_id": "one-session", "mode": "team.work.normal"}),
    (ReqMethod.MODE_SET, "web", {"session_id": "one-session", "mode": "team.work.normal", "expected_mode": None}),
    (ReqMethod.MODE_SET, "web", {"session_id": "one-session", "mode": "team.work.normal", "expected_mode": " "}),
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


@pytest.mark.asyncio
async def test_concurrent_conflicting_mode_sets_commit_one_and_refuse_the_other(
    mode_session, session_channel, monkeypatch,
):
    """Two tabs switch one Session at once from the same view: one commits, the stale one conflicts."""
    from jiuwenswarm.server import agent_ws_server

    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    responses = []

    async def send_wire(_ws, payload):
        responses.append(payload)

    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    server._mode_request_locks = WeakValueDictionary()
    monkeypatch.setattr(server, "_execution_runtime", lambda: runtime)
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    monkeypatch.setattr(
        agent_ws_server, "encode_agent_response_for_wire",
        lambda response, response_id=None: response,
    )

    def request(request_id, target):
        return AgentRequest(
            request_id=request_id,
            channel_id=session_channel,
            session_id="one-session",
            user_id="user-1",
            req_method=ReqMethod.MODE_SET,
            params={"session_id": "one-session", "mode": target, "expected_mode": "agent.code.normal"},
        )

    _, revision_before = _persisted_mode()
    send_lock = asyncio.Lock()
    try:
        await asyncio.wait_for(asyncio.gather(
            server._handle_mode_set(None, request("tab-a", "team.work.normal"), send_lock),
            server._handle_mode_set(None, request("tab-b", "agent.work.normal"), send_lock),
        ), timeout=5)
    finally:
        await runtime.close()

    committed = [response for response in responses if response.ok]
    refused = [response for response in responses if not response.ok]
    assert len(committed) == 1 and len(refused) == 1
    assert refused[0].payload["code"] == "MODE_CONFLICT"
    mode, revision = _persisted_mode()
    assert mode == committed[0].payload["mode"]
    assert session_metadata._mode_revision(revision) == session_metadata._mode_revision(revision_before) + 1


@pytest.mark.parametrize("target", ["team.design", "team", "agent.work", "agent.work.bogus", ""])
def test_mode_set_refuses_a_target_that_is_not_a_canonical_switchable_mode(mode_session, session_channel, target):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    before = _persisted_mode()
    with pytest.raises(SessionModeSwitchError) as refused:
        _switch(runtime, session_channel, target, "agent.code.normal")
    assert refused.value.code == "BAD_REQUEST"
    assert _persisted_mode() == before


def test_mode_requests_for_an_unknown_session_are_not_found(mode_session, session_channel):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as switch:
        runtime.switch_session_mode(
            session_id="no-such-session", target_mode="team.work.normal", expected_mode="agent.code.normal",
            channel_id=session_channel, user_id="user-1",
        )
    assert switch.value.code == "NOT_FOUND"
    with pytest.raises(SessionModeSwitchError) as read:
        AgentRuntime.get_session_mode(session_id="no-such-session", channel_id=session_channel, user_id="user-1")
    assert read.value.code == "NOT_FOUND"


# --- Only the recorded owner may switch; a blank owner is the anonymous user ------

def _set_stored_owner(owner):
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored["user_id"] = owner
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")
    session_metadata.remove_session_metadata_cache("one-session")


def _stored_owner():
    metadata_path = session_metadata.get_agent_sessions_dir() / "one-session" / "metadata.json"
    return json.loads(metadata_path.read_text(encoding="utf-8")).get("user_id")


@pytest.mark.parametrize("stored, caller, visible", [
    ("user-1", "user-1", True),
    ("user-1", "user-2", False),
    ("user-1", "", False),
    ("", "", True),
    (None, "", True),
    ("", "user-1", False),
    (" ", "user-1", False),
])
def test_mode_switch_caller_must_be_the_recorded_owner(session_channel, stored, caller, visible):
    """A blank owner is the anonymous user: only an anonymous caller (the single-user edition) matches it."""
    metadata = {"channel_id": session_channel, "user_id": stored}
    assert session_metadata.mode_switch_session_visible_to(
        metadata, channel_id=session_channel, user_id=caller,
    ) is visible


def test_identified_caller_cannot_switch_or_read_an_ownerless_session(mode_session, session_channel, monkeypatch):
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    _set_stored_owner("")
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())

    with pytest.raises(SessionModeSwitchError) as switch:
        _switch(runtime, session_channel, "team.work.normal", "agent.code.normal", user_id="user-1")
    assert switch.value.code == "NOT_FOUND"
    with pytest.raises(SessionModeSwitchError) as read:
        _get_mode(session_channel, user_id="user-1")
    assert read.value.code == "NOT_FOUND"
    assert _persisted_mode()[0] == "agent.code.normal"

    # The single-user edition's anonymous caller still owns its own Sessions.
    switched = _switch(runtime, session_channel, "team.work.normal", "agent.code.normal", user_id="")
    assert switched["mode"] == "team.work.normal"
    assert _get_mode(session_channel, user_id="")["mode"] == "team.work.normal"


def test_a_prompt_from_an_identified_user_does_not_claim_an_ownerless_session(
    mode_session, session_channel, monkeypatch,
):
    """Owners are fixed at creation; a prompt cannot grant what mode.set would have refused."""
    from jiuwenswarm.runtime.request import sync_chat_request_metadata

    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    _set_stored_owner("")
    prompt = AgentRequest(
        request_id="claim-1", channel_id=session_channel, session_id="one-session", user_id="user-2",
        req_method=ReqMethod.CHAT_SEND, params={"query": "hello"},
    )
    sync_chat_request_metadata(prompt, None, "agent.code.normal", user_id="user-2")
    session_metadata.flush_pending_writes()
    assert _stored_owner() == ""

    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as switch:
        _switch(runtime, session_channel, "team.work.normal", "agent.code.normal", user_id="user-2")
    assert switch.value.code == "NOT_FOUND"


def test_a_session_created_by_a_request_still_records_its_owner(mode_session, session_channel):
    """The request sync's own creation path keeps recording the creator."""
    from jiuwenswarm.runtime.request import sync_chat_request_metadata

    prompt = AgentRequest(
        request_id="create-1", channel_id=session_channel, session_id="fresh-session", user_id="user-2",
        req_method=ReqMethod.CHAT_SEND, params={"query": "hello"},
    )
    try:
        sync_chat_request_metadata(prompt, None, "agent.code.normal", user_id="user-2")
        session_metadata.flush_pending_writes()
        stored = session_metadata.get_session_metadata("fresh-session", cache_bust=True, enable_writeback=False)
        assert stored["user_id"] == "user-2"
    finally:
        session_metadata.remove_session_metadata_cache("fresh-session")
