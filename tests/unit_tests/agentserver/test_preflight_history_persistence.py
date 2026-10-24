# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pre-flight failures must not lose the user's turn.

Regression cover for two defects that combined to make a Team-mode session
reopen permanently empty:

1. ``server/pipeline.py`` ran hooks, table dispatch and auto team binding *before*
   ``_handle_stream``, which is where the adapter persists the user turn. Any
   failure there dropped the request with nothing on disk, so ``history.get``
   reported "session history not found" and the session looked blank forever.
2. Auto team naming had no degradation path, so a failing ``generate_team_name``
   also left ``metadata.team_name`` empty -- poisoning every later turn of that
   session the same way.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode
from jiuwenswarm.server.runtime.session.session_history import (
    _USER_HISTORY_PERSISTED_ATTR,
    append_history_record,
    flush_session_history,
    load_history_records,
    mark_user_history_persisted,
    user_history_persisted,
)

# Reuse the canonical patching helpers: ``_sessions_dir_for_request`` is bound by
# from-import in several handler modules, so patching one module silently misses
# the others and the test writes into the real user workspace.
from tests.unit_tests.agentserver.test_agentserver_acp import (
    patch_session_roots,
    patch_shared_name,
)


TEAM_MODES = ["team", "team.plan", "code.team"]


def _chat_send_request(session_id: str, query: str, *, mode: str = "team") -> AgentRequest:
    return AgentRequest(
        request_id="req-preflight",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"mode": mode, "query": query},
    )


def _records(session_id: str, sessions_root) -> list[dict]:
    """Read history after draining the async writer (writes are queued)."""
    flush_session_history(session_id, sessions_root=str(sessions_root))
    return load_history_records(session_id, sessions_root=sessions_root)


def _restored_events(session_id: str, sessions_root) -> list[str]:
    """event types that ``history.get`` would actually hand back to the UI."""
    flush_session_history(session_id, sessions_root=str(sessions_root))
    from jiuwenswarm.server.handlers.session import get_conversation_history

    page = get_conversation_history(
        session_id=session_id,
        page_idx=1,
        sessions_root=str(sessions_root),
    )
    assert page is not None, "history.get returned nothing for a session that has records"
    return [item.get("event_type") for item in page["messages"]]


class _RecordingSink:
    def __init__(self) -> None:
        self.wires: list = []

    async def send_wire(self, wire) -> None:
        self.wires.append(wire)


class _DispatchContext:
    """Minimal ``RequestContext`` for the non-ACP error path."""

    def __init__(self) -> None:
        self.sink = _RecordingSink()
        self.connection_id = "test-connection"
        self.services = None


# ---------------------------------------------------------------------------
# Layer 1: pre-flight failure persists the user turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", TEAM_MODES)
async def test_preflight_failure_persists_user_turn_and_chat_error(
    monkeypatch, tmp_path, mode
):
    """A failing pre-flight step must still leave the turn on disk, restorable."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-preflight", "你好", mode=mode)

    pipeline._persist_preflight_turn(
        request, exc=RuntimeError("team_name generation failed")
    )

    records = _records("sess-preflight", sessions_root)
    assert [r["role"] for r in records] == ["user", "assistant"]
    assert records[0]["content"] == "你好"
    assert records[0]["mode"] == mode
    assert records[1]["event_type"] == "chat.error"
    assert "team_name generation failed" in records[1]["content"]
    assert records[1]["error_type"] == "RuntimeError"

    # The user must see both turns after reopening the session.
    assert "chat.error" in _restored_events("sess-preflight", sessions_root)


@pytest.mark.asyncio
async def test_preflight_failure_does_not_duplicate_an_already_persisted_turn(
    monkeypatch, tmp_path
):
    """The adapter marks the request once it wrote the turn; respect that."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-guard", "already stored")

    append_history_record(
        session_id="sess-guard",
        request_id=request.request_id,
        channel_id=request.channel_id,
        role="user",
        content="already stored",
        timestamp=1.0,
        mode="team",
        sessions_root=sessions_root,
    )
    mark_user_history_persisted(request)
    assert user_history_persisted(request) is True

    pipeline._persist_preflight_turn(request, exc=RuntimeError("later step failed"))

    records = _records("sess-guard", sessions_root)
    assert [r["role"] for r in records] == ["user"]


@pytest.mark.asyncio
async def test_preflight_failure_skips_requests_that_are_not_user_turns(
    monkeypatch, tmp_path
):
    """Non-chat RPCs and suppressed turns must not gain a bogus user record."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)

    listed = AgentRequest(
        request_id="req-list",
        channel_id="web",
        session_id="sess-list",
        req_method=ReqMethod.SESSION_LIST,
        params={},
    )
    pipeline._persist_preflight_turn(listed, exc=RuntimeError("nope"))
    assert _records("sess-list", sessions_root) == []

    suppressed = _chat_send_request("sess-suppressed", "系统消息")
    suppressed.params["log_as_user"] = False
    pipeline._persist_preflight_turn(suppressed, exc=RuntimeError("nope"))
    assert _records("sess-suppressed", sessions_root) == []


@pytest.mark.asyncio
async def test_preflight_persist_survives_a_broken_history_backend(monkeypatch, tmp_path):
    """Persistence trouble must not replace the original error."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-broken-history", "hello")

    def explode(**kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        explode,
    )
    # Must not raise: the caller is already surfacing the real failure.
    pipeline._persist_preflight_turn(request, exc=RuntimeError("original failure"))


def test_user_history_persisted_marker_is_process_local():
    """The marker must live outside ``metadata``.

    ``metadata`` is copied verbatim onto the wire and persisted into
    ``metadata.json``; an internal flag stored there would both leak to clients
    and let a caller forge the flag to suppress the fallback write.
    """
    request = AgentRequest(
        request_id="req-no-metadata",
        channel_id="web",
        session_id="sess-no-metadata",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
    )
    assert user_history_persisted(request) is False
    mark_user_history_persisted(request)
    assert user_history_persisted(request) is True
    # Must not surface on any client- or storage-facing channel.
    assert request.metadata is None
    assert getattr(request, _USER_HISTORY_PERSISTED_ATTR) is True


def test_user_history_persisted_marker_leaves_metadata_untouched():
    """Existing ``metadata`` survives verbatim -- no marker key is injected."""
    request = AgentRequest(
        request_id="req-with-metadata",
        channel_id="web",
        session_id="sess-with-metadata",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
        metadata={"user_id": "u1", "mode": "team"},
    )
    mark_user_history_persisted(request)
    assert user_history_persisted(request) is True
    assert request.metadata == {"user_id": "u1", "mode": "team"}


def test_forged_metadata_cannot_suppress_the_fallback_write():
    """A caller-supplied marker key must not defeat the fallback path.

    The old metadata-based marker let an inbound frame pre-set the key and
    suppress the pre-flight fallback, silently dropping the user's turn.
    """
    request = _chat_send_request("sess-forged", "建立一个团队")
    request.metadata = {"jiuwenswarm.user_history_persisted": True}
    assert user_history_persisted(request) is False


# ---------------------------------------------------------------------------
# Layer 2: auto team naming degrades instead of poisoning the session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", TEAM_MODES)
def test_fallback_team_name_is_deterministic_and_storage_safe(mode):
    from jiuwenswarm.server.handlers.team import _fallback_team_name
    from jiuwenswarm.server.runtime.team_binding_store import validate_team_name

    name = _fallback_team_name("sess-auto-team")
    assert name == _fallback_team_name("sess-auto-team")
    assert name != _fallback_team_name("sess-other-team")
    # validate_team_name raises TeamBindingStoreError on a bad shape.
    assert validate_team_name(name) == name
    assert "/" not in name and "\\" not in name
    assert len(name) <= 64


@pytest.mark.parametrize("mode", TEAM_MODES)
@pytest.mark.asyncio
async def test_auto_team_binding_falls_back_when_name_generation_fails(
    monkeypatch, tmp_path, mode
):
    """Naming failure must still bind a team and persist it, not re-poison."""
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.agents.harness.team import TeamNameGenerationError
    from jiuwenswarm.server.handlers.chat import _ensure_auto_team_binding_for_chat
    from jiuwenswarm.server.handlers.team import _fallback_team_name
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        init_session_metadata,
    )
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                }
            }
        }
    }

    async def failing_generate_team_name(*args, **kwargs):
        raise TeamNameGenerationError("APIError: An error occurred during streaming")

    patch_shared_name(monkeypatch, "get_config", lambda: config)
    monkeypatch.setattr(team_module, "generate_team_name", failing_generate_team_name)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    session_id = "sess-fallback-team"
    init_session_metadata(session_id=session_id, channel_id="web", mode=mode)
    assert get_session_metadata(session_id, cache_bust=True)["team_name"] == ""

    request = _chat_send_request(session_id, "建立一个团队", mode=mode)
    binding = await _ensure_auto_team_binding_for_chat(None, request)

    expected = _fallback_team_name(session_id)
    assert binding.team_name == expected
    assert request.params["team_name"] == expected
    assert request.params["team_template_id"] == "research"
    # The original query is untouched.
    assert request.params["query"] == "建立一个团队"

    # Persisted, so the next turn reuses the binding instead of re-running TinyAgent.
    persisted = get_session_metadata(session_id, cache_bust=True)
    assert persisted["team_name"] == expected
    assert persisted["runtime_team_name"] == f"{expected}_{session_id}"
    assert persisted["team_template_id"] == "research"
    assert binding_store.get(expected).session_ids == (session_id,)
    assert entity_store.get(expected) is not None

    # A second turn must not invoke the failing generator again; the already-bound
    # branch returns the team name itself (chat.py `return existing_team_name`).
    second = _chat_send_request(session_id, "继续", mode=mode)
    again = await _ensure_auto_team_binding_for_chat(None, second)
    assert again == expected


@pytest.mark.asyncio
async def test_auto_team_binding_still_raises_when_no_template_is_configured(
    monkeypatch, tmp_path
):
    """A genuinely broken team config must stay visible, not be masked."""
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.server.handlers.chat import _ensure_auto_team_binding_for_chat
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)

    async def failing_generate_team_name(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(team_module, "generate_team_name", failing_generate_team_name)
    patch_shared_name(monkeypatch, "get_config", lambda: {"modes": {"team": {}}})

    session_id = "sess-no-template"
    init_session_metadata(session_id=session_id, channel_id="web", mode="team")
    request = _chat_send_request(session_id, "hi")

    with pytest.raises(TeamBindingStoreError):
        await _ensure_auto_team_binding_for_chat(None, request)


@pytest.mark.asyncio
async def test_auto_team_binding_keeps_generated_name_on_success(monkeypatch, tmp_path):
    """The happy path must be untouched: generated names still win."""
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.server.handlers.chat import _ensure_auto_team_binding_for_chat
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                }
            }
        }
    }

    async def generated_team_name(*args, **kwargs):
        return "landlord-game"

    monkeypatch.setattr(team_module, "generate_team_name", generated_team_name)
    patch_shared_name(monkeypatch, "get_config", lambda: config)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    session_id = "sess-generated-team"
    init_session_metadata(session_id=session_id, channel_id="web", mode="team")
    request = _chat_send_request(session_id, "hi")

    binding = await _ensure_auto_team_binding_for_chat(None, request)
    assert binding.team_name == "landlord-game"


def test_mode_canonicalization_matches_team_fallback_gate():
    """Guard: the modes covered above are the ones that trigger auto binding."""
    from jiuwenswarm.server.handlers._shared import _is_team_metadata_mode

    for mode in TEAM_MODES:
        _, _, canonical = resolve_agent_request_mode(mode)
        assert _is_team_metadata_mode({"mode": canonical}) is True
    _, _, agent_mode = resolve_agent_request_mode("agent")
    assert _is_team_metadata_mode({"mode": agent_mode}) is False


@pytest.mark.asyncio
async def test_preflight_failure_helper_is_reentrant_across_turns(monkeypatch, tmp_path):
    """Two failed turns in one session must produce two distinct pairs."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)

    for index, query in enumerate(["first", "second"]):
        request = AgentRequest(
            request_id=f"req-{index}",
            channel_id="web",
            session_id="sess-multi",
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={"mode": "team", "query": query},
        )
        pipeline._persist_preflight_turn(request, exc=RuntimeError(f"boom {index}"))

    records = _records("sess-multi", sessions_root)
    assert [r["content"] for r in records] == [
        "first",
        "boom 0",
        "second",
        "boom 1",
    ]
    assert {r["request_id"] for r in records} == {"req-0", "req-1"}


@pytest.mark.asyncio
async def test_preflight_failure_creates_the_history_file_a_reopened_session_needs(
    monkeypatch, tmp_path
):
    """The reported bug: a session dir with metadata but no history at all."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-new", "hi")

    assert not (sessions_root / "sess-new" / "history.json").exists()
    pipeline._persist_preflight_turn(request, exc=RuntimeError("simulated"))

    assert (sessions_root / "sess-new" / "history.json").exists()
    assert _restored_events("sess-new", sessions_root)


@pytest.mark.asyncio
async def test_dispatch_failure_after_hook_persists_turn(monkeypatch, tmp_path):
    """End-to-end: the real dispatch path, not just the storage helper."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-dispatch", "team please")

    async def pass_hook(_request):
        return None

    async def not_dispatched(_ctx, _request):
        return False

    async def fail_binding(_ctx, _request):
        raise RuntimeError("team binding failed")

    monkeypatch.setattr(pipeline, "_trigger_before_chat_request_hook", pass_hook)
    monkeypatch.setattr(pipeline, "dispatch_with_context", not_dispatched)
    monkeypatch.setattr(pipeline, "_ensure_auto_team_binding_for_chat", fail_binding)

    ctx = _DispatchContext()
    await pipeline.dispatch_parsed_request(ctx, request)

    records = _records("sess-dispatch", sessions_root)
    assert [r["role"] for r in records] == ["user", "assistant"]
    assert records[0]["content"] == "team please"
    assert records[1]["event_type"] == "chat.error"
    assert "team binding failed" in records[1]["content"]
    assert len(ctx.sink.wires) == 1


@pytest.mark.asyncio
async def test_hook_rejection_does_not_persist_the_turn(monkeypatch, tmp_path):
    """Policy rejections must not write the blocked content to history."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-hook-blocked", "blocked text")

    async def block_hook(_request):
        raise RuntimeError("blocked by policy")

    monkeypatch.setattr(pipeline, "_trigger_before_chat_request_hook", block_hook)

    ctx = _DispatchContext()
    await pipeline.dispatch_parsed_request(ctx, request)

    assert _records("sess-hook-blocked", sessions_root) == []
    assert len(ctx.sink.wires) == 1


# ---------------------------------------------------------------------------
# Layer 3: cancellation, disconnects, quota and record shape
# ---------------------------------------------------------------------------


def _patch_preflight_path(monkeypatch, *, binding, hook=None):
    """Drive the real dispatch path with a stubbed pre-flight step."""
    from jiuwenswarm.server import pipeline

    async def pass_hook(_request):
        return None

    async def not_dispatched(_ctx, _request):
        return False

    monkeypatch.setattr(
        pipeline,
        "_trigger_before_chat_request_hook",
        hook if hook is not None else pass_hook,
    )
    monkeypatch.setattr(pipeline, "dispatch_with_context", not_dispatched)
    monkeypatch.setattr(pipeline, "_ensure_auto_team_binding_for_chat", binding)


@pytest.mark.asyncio
async def test_cancel_after_hook_persists_only_user_turn(monkeypatch, tmp_path):
    """Stopping during the naming call must leave the sent message on disk."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-cancel", "建个团队")

    async def cancel_binding(_ctx, _request):
        raise asyncio.CancelledError()

    _patch_preflight_path(monkeypatch, binding=cancel_binding)

    with pytest.raises(asyncio.CancelledError):
        await pipeline.dispatch_parsed_request(_DispatchContext(), request)

    records = _records("sess-cancel", sessions_root)
    assert [r["role"] for r in records] == ["user"]
    assert records[0]["content"] == "建个团队"
    # A user stop is not a model failure: no error bubble in the transcript.
    assert "event_type" not in records[0]


@pytest.mark.asyncio
async def test_disconnect_after_hook_persists_only_user_turn(monkeypatch, tmp_path):
    """A dropped client must not leave an empty session behind either."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-disconnect", "建个团队")

    async def disconnect_binding(_ctx, _request):
        raise WebSocketConnectionClosed(None, None)

    _patch_preflight_path(monkeypatch, binding=disconnect_binding)

    ctx = _DispatchContext()
    await pipeline.dispatch_parsed_request(ctx, request)

    records = _records("sess-disconnect", sessions_root)
    assert [r["role"] for r in records] == ["user"]
    assert records[0]["content"] == "建个团队"
    # The client is gone, so there is nothing to send back.
    assert ctx.sink.wires == []


@pytest.mark.asyncio
async def test_cancel_before_hook_persists_nothing(monkeypatch, tmp_path):
    """A hook that has not passed yet must not leak content into history."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-cancel-blocked", "blocked text")

    async def block_hook(_request):
        raise asyncio.CancelledError()

    async def never_binding(_ctx, _request):
        return None

    _patch_preflight_path(monkeypatch, binding=never_binding, hook=block_hook)

    with pytest.raises(asyncio.CancelledError):
        await pipeline.dispatch_parsed_request(_DispatchContext(), request)

    assert _records("sess-cancel-blocked", sessions_root) == []


@pytest.mark.asyncio
async def test_cancel_does_not_duplicate_an_already_persisted_turn(
    monkeypatch, tmp_path
):
    """The adapter may have written the turn before the cancellation landed."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-cancel-stored", "already stored")

    append_history_record(
        session_id="sess-cancel-stored",
        request_id=request.request_id,
        channel_id=request.channel_id,
        role="user",
        content="already stored",
        timestamp=1.0,
        mode="team",
        sessions_root=sessions_root,
    )
    mark_user_history_persisted(request)

    pipeline._persist_preflight_turn(request)

    assert [r["role"] for r in _records("sess-cancel-stored", sessions_root)] == [
        "user"
    ]


def test_quota_rejection_skips_preflight_write(monkeypatch, tmp_path):
    """The fallback write must honour the same workspace quota gate."""
    from jiuwenswarm.common.workspace.quota import WorkspaceQuotaExceeded
    from jiuwenswarm.server.runtime.agent_adapter.interface import record_failed_turn

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-quota", "hi")

    def deny(**_kwargs):
        raise WorkspaceQuotaExceeded()

    monkeypatch.setattr(
        "jiuwenswarm.common.workspace.quota.check_workspace_write",
        deny,
    )

    assert record_failed_turn(request, RuntimeError("boom")) is False
    assert _records("sess-quota", sessions_root) == []


@pytest.mark.asyncio
async def test_multimodal_preflight_turn_is_persisted(monkeypatch, tmp_path):
    """A turn with attachments but no text used to be dropped entirely."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = AgentRequest(
        request_id="req-media",
        channel_id="web",
        session_id="sess-media",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "mode": "team",
            "query": "",
            "media_items": [{"path": "/tmp/cat.png", "type": "image"}],
        },
    )

    pipeline._persist_preflight_turn(request, exc=RuntimeError("boom"))

    records = _records("sess-media", sessions_root)
    assert [r["role"] for r in records] == ["user", "assistant"]
    assert records[0]["content"] == ""
    assert records[0]["media_items"][0]["filename"] == "cat.png"
    assert records[1]["event_type"] == "chat.error"


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_turn_is_not_persisted(monkeypatch, tmp_path, query):
    """Neither an empty nor a whitespace-only turn deserves a history row."""
    from jiuwenswarm.server import pipeline

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-empty", query)

    pipeline._persist_preflight_turn(request, exc=RuntimeError("boom"))

    assert _records("sess-empty", sessions_root) == []


@pytest.mark.asyncio
async def test_cancelled_turn_is_visible_after_reload(monkeypatch, tmp_path):
    """A user-only record must survive the same restore path the UI uses."""
    from jiuwenswarm.server import pipeline
    from jiuwenswarm.server.handlers.session import get_conversation_history

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    request = _chat_send_request("sess-cancel-reload", "还在吗")
    pipeline._persist_preflight_turn(request)

    flush_session_history("sess-cancel-reload", sessions_root=str(sessions_root))
    page = get_conversation_history(
        session_id="sess-cancel-reload",
        page_idx=1,
        sessions_root=str(sessions_root),
    )
    assert page is not None
    assert [item.get("role") for item in page["messages"]] == ["user"]
    assert page["messages"][0]["content"] == "还在吗"


def test_error_text_is_bounded_and_single_line():
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _MAX_TURN_ERROR_CHARS,
        _failed_turn_error_text,
    )

    text = _failed_turn_error_text(RuntimeError("line one\nline two"))
    assert text == "line one line two"

    long_text = _failed_turn_error_text(RuntimeError("x" * 5000))
    assert len(long_text) == _MAX_TURN_ERROR_CHARS + 1
    assert long_text.endswith("…")
    assert "\n" not in long_text


def test_marker_write_is_not_swallowed():
    """A request shape that cannot hold the marker must fail loudly.

    Swallowing the error would silently degrade into duplicated history records.
    """
    request = _chat_send_request("sess-marker", "hi")
    mark_user_history_persisted(request)
    assert user_history_persisted(request) is True

    class SlottedRequest:
        __slots__ = ("request_id",)

        def __init__(self) -> None:
            self.request_id = "req-slotted"

    with pytest.raises(AttributeError):
        mark_user_history_persisted(SlottedRequest())


def test_bind_unique_team_suffixes_on_conflict(monkeypatch):
    """Conflict retry lives in one place for both naming paths."""
    from jiuwenswarm.server.handlers import team as team_module
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

    attempts: list[str] = []

    def create(*, team_name, template_id, config_base):
        attempts.append(team_name)
        if len(attempts) <= 2:
            raise TeamBindingStoreError("team_name already exists", code="CONFLICT")
        return team_name

    monkeypatch.setattr(team_module, "_create_team_binding_from_template", create)

    assert (
        team_module._bind_unique_team("landlord-game", template_id="t", config_base={})
        == "landlord-game_3"
    )
    assert attempts == ["landlord-game", "landlord-game_2", "landlord-game_3"]


def test_bind_unique_team_gives_up_after_repeated_conflicts(monkeypatch):
    from jiuwenswarm.server.handlers import team as team_module
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

    def always_conflict(**_kwargs):
        raise TeamBindingStoreError("team_name already exists", code="CONFLICT")

    monkeypatch.setattr(team_module, "_create_team_binding_from_template", always_conflict)

    with pytest.raises(TeamBindingStoreError) as excinfo:
        team_module._bind_unique_team("landlord-game", template_id="t", config_base={})
    assert excinfo.value.code == "CONFLICT"


def test_bind_unique_team_propagates_other_errors(monkeypatch):
    from jiuwenswarm.server.handlers import team as team_module
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

    def missing_template(**_kwargs):
        raise TeamBindingStoreError("template_id not found", code="NOT_FOUND")

    monkeypatch.setattr(
        team_module, "_create_team_binding_from_template", missing_template
    )

    with pytest.raises(TeamBindingStoreError) as excinfo:
        team_module._bind_unique_team("landlord-game", template_id="t", config_base={})
    assert excinfo.value.code == "NOT_FOUND"


def test_fallback_team_name_uses_injected_day():
    from jiuwenswarm.server.handlers.team import _fallback_team_name
    from jiuwenswarm.server.runtime.team_binding_store import validate_team_name

    digest = hashlib.sha256(b"sess-day").hexdigest()[:6]
    name = _fallback_team_name("sess-day", today="20261020")

    assert name == f"team-20261020-{digest}"
    assert _fallback_team_name("sess-day", today="20261020") == name
    assert _fallback_team_name("sess-day", today="20261021") != name
    assert validate_team_name(name) == name


def test_team_name_generation_error_reason_defaults_to_generation():
    from jiuwenswarm.agents.harness.team import TeamNameGenerationError

    assert TeamNameGenerationError("timed out").reason == (
        TeamNameGenerationError.REASON_GENERATION
    )
    assert TeamNameGenerationError(
        "no model", reason=TeamNameGenerationError.REASON_CONFIG
    ).reason == TeamNameGenerationError.REASON_CONFIG
    # Existing `except RuntimeError` callers keep working.
    assert isinstance(TeamNameGenerationError("x"), RuntimeError)


@pytest.mark.asyncio
async def test_auto_team_binding_keeps_config_errors_fatal(monkeypatch, tmp_path):
    """A missing model is an operator error: do not hide it behind a hash name."""
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.agents.harness.team import TeamNameGenerationError
    from jiuwenswarm.server.handlers.chat import _ensure_auto_team_binding_for_chat
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                }
            }
        }
    }

    async def config_error(*_args, **_kwargs):
        raise TeamNameGenerationError(
            "default team model_name is missing",
            reason=TeamNameGenerationError.REASON_CONFIG,
        )

    monkeypatch.setattr(team_module, "generate_team_name", config_error)
    patch_shared_name(monkeypatch, "get_config", lambda: config)

    session_id = "sess-config-error"
    init_session_metadata(session_id=session_id, channel_id="web", mode="team")
    request = _chat_send_request(session_id, "hi")

    with pytest.raises(TeamNameGenerationError) as excinfo:
        await _ensure_auto_team_binding_for_chat(None, request)
    assert excinfo.value.reason == TeamNameGenerationError.REASON_CONFIG

