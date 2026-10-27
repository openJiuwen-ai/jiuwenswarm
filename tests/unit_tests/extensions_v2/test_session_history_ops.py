# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real user packages, append policy, committed files and stable history pages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading

import pytest
from openjiuwen.core.events import Event, EventHandlerOpsBuilder, SessionContext

from jiuwenswarm.common import utils
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.handler_ops.session_ops import session_messages
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.server.runtime.session import lifecycle, project_store, session_history, session_metadata
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError
from .management.support import write_package


USER_EXTENSION = '''from dataclasses import replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_history import SessionAppendRejection, SessionAppendResult


class Extension:
    def register(self, registrar):
        registrar.register_event_handler("ui.ready", self.query, {})
        registrar.register_event_handler("session.append", self.policy, {})

    async def query(self, event, ops, next_handler):
        request = event.event_input.payload
        try:
            receipt = None
            if request.get("message") is not None:
                receipt = await ops.session.append(request["message"])
            page = await ops.session.messages(request.get("limit", 50), request.get("cursor"))
            core = await next_handler(event)
            return {"receipt": receipt, "page": page, "accepted": core.accepted if hasattr(core, "accepted") else core["accepted"]}
        except Exception as error:
            return {"error": getattr(error, "code", type(error).__name__)}

    async def policy(self, event, ops, next_handler):
        body = event.event_input.content
        if body == "deny":
            return SessionAppendRejection("Policy refused the message.")
        if body == "recursive":
            try:
                await ops.session.append({"id": "inner", "role": "assistant", "content": "inner"})
            except Exception as error:
                return await next_handler(Event(event.event_type, replace(event.event_input, content=error.code)))
        if body == "invalid-route":
            return await next_handler(Event(event.event_type, replace(event.event_input, message_id="forged", role="user")))
        if body == "fake-success":
            return SessionAppendResult("persisted", "other-session", "fake")
        if body == "read-before":
            page = await ops.session.messages()
            return await next_handler(Event(event.event_type, replace(event.event_input, content="previous=" + str(len(page["messages"])))))
        if body == "throw":
            raise RuntimeError("A broken user handler is not a policy rejection")
        if body.startswith(("rewrite:", "platform:")):
            return await next_handler(Event(event.event_type, replace(event.event_input, content="[checked] " + body)))
        receipt = await next_handler(event)
        if body == "twice":
            return await next_handler(event)
        if body == "late-reject":
            return SessionAppendRejection("Too late to undo a commit")
        if body == "mutate-receipt":
            receipt.message["content"] = "FORGED RESULT"
        return receipt


def create_extension():
    return Extension()
'''


@pytest.fixture
async def history_host(tmp_path: Path, monkeypatch):
    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    for module in (session_history, session_metadata, lifecycle, project_store, utils):
        if hasattr(module, "get_agent_sessions_dir"):
            monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: sessions)
        if hasattr(module, "get_agent_root_dir"):
            monkeypatch.setattr(module, "get_agent_root_dir", lambda: sessions.parent)
    monkeypatch.setattr(session_metadata, "_METADATA_CACHE", {})
    monkeypatch.setattr(session_metadata, "_METADATA_CACHE_GENERATIONS", {})
    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    for sid in ("hist-a", "hist-b"):
        directory = sessions / sid
        directory.mkdir()
        (directory / "metadata.json").write_text(json.dumps({"session_id": sid, "title": "Existing title", "mode": "agent"}), encoding="utf-8")
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "store")
    host = await assembly.install_extensions()
    assert host is not None
    package = write_package(tmp_path / "source", package_id="history.reader", module_source=USER_EXTENSION)
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    try:
        yield host, sessions
    finally:
        await assembly.shutdown_extensions()
        await asyncio.to_thread(session_history.flush_history_writes)
        assert await asyncio.to_thread(session_metadata.flush_pending_writes)


async def _query(history_host, *, sid="hist-a", message=None, limit=50, cursor=None):
    host, _ = history_host
    payload = {"limit": limit, "cursor": cursor}
    if message is not None:
        payload["message"] = message
    event = Event("ui.ready", UiActionInput("ui.ready", RenderTarget(sid, "chat.header"), payload))
    return await host.bus.trigger(event, SessionContext(sid))


def _message(content="hello", identifier="note", role="assistant"):
    return {"id": identifier, "role": role, "content": content}


def _rows(sessions: Path, sid="hist-a"):
    path = sessions / sid / "history.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _seed(sessions: Path, count=3, sid="hist-a"):
    path = sessions / sid / "history.jsonl"
    rows = [{"id": f"seed-{i}", "role": "user", "content": f"seed-{i}", "timestamp": i} for i in range(count)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.asyncio
async def test_real_user_package_rewrites_commits_and_reads_without_workspace(history_host):
    _, sessions = history_host
    metadata = (sessions / "hist-a" / "metadata.json").read_bytes()
    result = await _query(history_host, message=_message("rewrite:审计说明"))
    receipt = result["receipt"]
    assert result["accepted"] and receipt["status"] == "persisted"
    assert receipt["message_id"] == "note" and receipt["session_id"] == "hist-a"
    assert receipt["message"]["content"] == "[checked] rewrite:审计说明"
    assert result["page"]["messages"][0] == receipt["message"]
    assert "_session_append" not in receipt["message"]
    assert _rows(sessions)[0]["content"] == receipt["message"]["content"]
    assert (sessions / "hist-a" / "metadata.json").read_bytes() == metadata


@pytest.mark.asyncio
async def test_explicit_rejection_creates_no_history_or_metadata_change(history_host):
    _, sessions = history_host
    before = (sessions / "hist-a" / "metadata.json").read_bytes()
    result = await _query(history_host, message=_message("deny"))
    assert result["receipt"]["status"] == "rejected"
    assert result["receipt"]["message"] is None and not _rows(sessions)
    assert before == (sessions / "hist-a" / "metadata.json").read_bytes()


@pytest.mark.asyncio
async def test_retry_returns_persisted_message_and_conflicting_original_is_refused(history_host):
    _, sessions = history_host
    first = await _query(history_host, message=_message("rewrite:once"))
    second = await _query(history_host, message=_message("rewrite:once"))
    assert first["receipt"]["status"] == "persisted" and second["receipt"]["status"] == "duplicate"
    assert first["receipt"]["message"] == second["receipt"]["message"]
    assert (await _query(history_host, message=_message("different")))["error"] == "APPEND_ID_CONFLICT"
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["twice", "late-reject", "mutate-receipt", "throw", "invalid-route"])
async def test_core_receipt_preserves_one_real_commit_under_handler_misbehavior(history_host, body):
    _, sessions = history_host
    result = await _query(history_host, message=_message(body))
    assert result["receipt"]["status"] == "persisted"
    assert result["receipt"]["message"]["content"] == body
    assert result["receipt"]["message"]["role"] == "assistant"
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
async def test_fabricated_success_cannot_replace_a_write(history_host):
    _, sessions = history_host
    assert (await _query(history_host, message=_message("fake-success")))["error"] == "APPEND_RESULT_INVALID"
    assert not _rows(sessions)


@pytest.mark.asyncio
async def test_recursive_append_is_refused_before_a_second_write(history_host):
    _, sessions = history_host
    result = await _query(history_host, message=_message("recursive"))
    assert result["receipt"]["message"]["content"] == "APPEND_REENTRANT"
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
async def test_cursor_keeps_old_snapshot_and_new_query_sees_append(history_host):
    _, sessions = history_host
    _seed(sessions)
    first = (await _query(history_host, limit=1))["page"]
    appended = await _query(history_host, message=_message("new"))
    old = (await _query(history_host, limit=10, cursor=first["next_cursor"]))["page"]
    assert [row["content"] for row in old["messages"]] == ["seed-1", "seed-0"]
    assert old["snapshot_id"] == first["snapshot_id"]
    assert appended["page"]["messages"][0]["content"] == "new"
    assert (await _query(history_host, limit=10, cursor=first["next_cursor"]))["page"] == old


@pytest.mark.asyncio
async def test_cursor_is_bound_to_session_and_replacement_invalidates_it(history_host):
    _, sessions = history_host
    _seed(sessions)
    cursor = (await _query(history_host, limit=1))["page"]["next_cursor"]
    assert (await _query(history_host, sid="hist-b", cursor=cursor))["error"] == "INVALID_CURSOR"
    path = sessions / "hist-a" / "history.jsonl"
    replacement = path.with_suffix(".new")
    replacement.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    replacement.replace(path)
    assert (await _query(history_host, cursor=cursor))["error"] == "SNAPSHOT_CHANGED"


@pytest.mark.asyncio
async def test_concurrent_sessions_and_packages_have_distinct_append_identities(history_host, tmp_path):
    host, sessions = history_host
    results = await asyncio.gather(*(_query(history_host, sid=sid, message=_message(sid)) for sid in ["hist-a", "hist-b"]))
    assert [r["receipt"]["session_id"] for r in results] == ["hist-a", "hist-b"]
    second_package = write_package(tmp_path / "other", package_id="other.reader", module_source=USER_EXTENSION)
    assert (await host.manager.install([second_package]))[0].status == "SUCCESS"
    await _query(history_host, message=_message("two packages", identifier="shared"))
    result = await _query(history_host)
    shared = [row for row in result["page"]["messages"] if row.get("extension_message_id") == "shared"]
    assert {row["extension_package_id"] for row in shared} == {"history.reader", "other.reader"}
    assert len({row["id"] for row in shared}) == 2
    assert len(_rows(sessions)) == 3 and len(_rows(sessions, "hist-b")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    {}, {"role": "assistant", "content": "no id"}, _message("", "empty"),
    _message("x", "bad/id"), _message("x", "x" * 129), _message("x", role="system"),
    {**_message(), "session_id": "hist-b"}, _message(42), _message("\ud800"),
])
async def test_invalid_user_message_is_rejected_before_writing(history_host, message):
    _, sessions = history_host
    assert (await _query(history_host, message=message))["error"] == "INVALID_MESSAGE"
    assert not _rows(sessions) and not _rows(sessions, "hist-b")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, 2.5, "5"])
async def test_invalid_page_limit_is_explicit(history_host, limit):
    assert (await _query(history_host, limit=limit))["error"] == "INVALID_MESSAGE_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", "malformed", 3, True])
async def test_invalid_cursor_is_explicit(history_host, cursor):
    assert (await _query(history_host, cursor=cursor))["error"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_read_requires_session_but_does_not_invent_missing_history(history_host):
    _, sessions = history_host
    with pytest.raises(RuntimeError, match="requires SessionContext"):
        await session_messages(EventHandlerOpsBuilder().build())
    result = await _query(history_host, sid="new-session")
    assert result["page"] == {"messages": [], "next_cursor": None, "has_more": False, "snapshot_id": None}
    assert not (sessions / "new-session").exists()
    assert (await _query(history_host, sid="new-session", message=_message()))["error"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_existing_awaitable_platform_write_is_modified_before_persistence(history_host):
    _, sessions = history_host
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="platform-1",
                         channel_id="web", role="assistant", content="platform:normal", timestamp=123.0, event_type="chat.final")
    assert _rows(sessions)[0]["content"] == "[checked] platform:normal"
    assert (await _query(history_host))["page"]["messages"][0]["content"] == "[checked] platform:normal"
    with pytest.raises(SessionHistoryError, match="APPEND_REJECTED"):
        await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="platform-2",
                             channel_id="web", role="assistant", content="deny", timestamp=124.0)
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
async def test_platform_display_timestamp_and_json_object_keys_remain_compatible(history_host):
    _, sessions = history_host
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="native-clock",
                         channel_id="web", role="assistant", content="platform:clock", timestamp=123.0,
                         extra={"timestamp": "2026-10-27T10:00:00Z", "details": {1: "one", "two": 2}})
    row = _rows(sessions)[0]
    assert row["timestamp"] == "2026-10-27T10:00:00Z"
    assert row["content"] == "[checked] platform:clock"
    assert row["details"] == {"1": "one", "two": 2}


@pytest.mark.asyncio
async def test_invalid_original_timestamp_is_rejected_before_write(history_host):
    _, sessions = history_host
    with pytest.raises(SessionHistoryError, match="INVALID_MESSAGE"):
        await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="bad-clock",
                             channel_id="web", role="assistant", content="hello", timestamp=float("nan"))
    assert not _rows(sessions)


@pytest.mark.asyncio
async def test_partial_write_failure_rolls_back_and_retry_can_commit(history_host, monkeypatch):
    _, sessions = history_host
    _seed(sessions)
    path = sessions / "hist-a" / "history.jsonl"
    before = path.read_bytes()
    original = session_history._append_record_unfenced

    def fail(target, record):
        with target.open("ab") as output:
            output.write(b'{"partial":')
        raise OSError("private filesystem details")

    monkeypatch.setattr(session_history, "_append_record_unfenced", fail)
    assert (await _query(history_host, message=_message()))["error"] == "HISTORY_IO_FAILED"
    assert path.read_bytes() == before
    monkeypatch.setattr(session_history, "_append_record_unfenced", original)
    assert (await _query(history_host, message=_message()))["receipt"]["status"] == "persisted"


@pytest.mark.asyncio
async def test_append_policy_can_read_history_without_holding_commit_locks(history_host):
    result = await asyncio.wait_for(_query(history_host, message=_message("read-before")), timeout=2)
    assert result["receipt"]["message"]["content"] == "previous=0"
    retry = await _query(history_host, message=_message("read-before"))
    assert retry["receipt"]["status"] == "duplicate"
    assert retry["receipt"]["message"]["content"] == "previous=0"


@pytest.mark.asyncio
async def test_failed_rollback_reports_an_unknown_outcome(history_host, monkeypatch):
    _, sessions = history_host
    _seed(sessions)
    original_open = Path.open

    def fail_append(path, record):
        with original_open(path, "ab") as output:
            output.write(b'{"partial":')
        raise OSError("write failed")

    def fail_rollback(path, mode="r", *args, **kwargs):
        if mode == "r+b" and path.name == "history.jsonl":
            raise OSError("rollback failed")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(session_history, "_append_record_unfenced", fail_append)
    monkeypatch.setattr(Path, "open", fail_rollback)
    assert (await _query(history_host, message=_message()))["error"] == "APPEND_OUTCOME_UNKNOWN"
    assert (sessions / "hist-a" / "history.jsonl").read_bytes().endswith(b'{"partial":')


@pytest.mark.asyncio
async def test_cancellation_drains_file_commit_and_same_id_retry_is_duplicate(history_host, monkeypatch):
    _, sessions = history_host
    entered, release = threading.Event(), threading.Event()
    original = session_history._append_record_unfenced

    def blocked(path, record):
        entered.set()
        assert release.wait(3)
        return original(path, record)

    monkeypatch.setattr(session_history, "_append_record_unfenced", blocked)
    task = asyncio.create_task(_query(history_host, message=_message()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(_rows(sessions)) == 1
    monkeypatch.setattr(session_history, "_append_record_unfenced", original)
    assert (await _query(history_host, message=_message()))["receipt"]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_cancellation_in_policy_has_no_write(history_host):
    host, sessions = history_host
    entered = asyncio.Event()

    async def blocked(event, ops, next_handler):
        entered.set()
        await asyncio.Event().wait()

    host.bus.register_event_handler("session.append", blocked, {})
    task = asyncio.create_task(_query(history_host, message=_message()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _rows(sessions)


@pytest.mark.asyncio
async def test_uninstall_removes_policy_but_keeps_default_platform_append(history_host):
    host, sessions = history_host
    assert (await host.manager.uninstall("history.reader")).status == "SUCCESS"
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="after-uninstall",
                         channel_id="web", role="assistant", content="platform:unchanged", timestamp=123.0)
    assert _rows(sessions)[0]["content"] == "platform:unchanged"


@pytest.mark.asyncio
async def test_reload_uses_persistent_identity_instead_of_module_state(history_host):
    host, sessions = history_host
    await _query(history_host, message=_message())
    assert (await host.manager.reload("history.reader")).status == "SUCCESS"
    assert (await _query(history_host, message=_message()))["receipt"]["status"] == "duplicate"
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
async def test_oversized_utf8_message_is_refused_without_truncation(history_host):
    _, sessions = history_host
    assert (await _query(history_host, message=_message("中" * 350000)))["error"] == "INVALID_MESSAGE"
    assert not _rows(sessions)


@pytest.mark.asyncio
async def test_lifecycle_denial_runs_before_user_policy(history_host):
    host, sessions = history_host
    observed = []

    async def observe(event, ops, next_handler):
        observed.append(True)
        return await next_handler(event)

    host.bus.register_event_handler("session.append", observe, {})
    with lifecycle.resource_lock("session", "hist-a"):
        lifecycle.save_locked("session", "hist-a", {"write_blocked": True, "generation": 1})
    assert (await _query(history_host, message=_message()))["error"] == "OPERATION_IN_PROGRESS"
    assert not observed and not _rows(sessions)


@pytest.mark.asyncio
async def test_generation_change_during_hook_is_fenced_at_commit(history_host):
    host, sessions = history_host

    async def change_generation(event, ops, next_handler):
        with lifecycle.resource_lock("session", "hist-a"):
            lifecycle.save_locked("session", "hist-a", {"generation": 1})
        return await next_handler(event)

    host.bus.register_event_handler("session.append", change_generation, {})
    assert (await _query(history_host, message=_message()))["error"] == "OPERATION_IN_PROGRESS"
    assert not _rows(sessions)


@pytest.mark.asyncio
async def test_explicit_legacy_writer_mode_keeps_its_write_but_refuses_cursor_query(history_host, monkeypatch):
    _, sessions = history_host
    monkeypatch.setenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", "1")
    result = await _query(history_host, message=_message())
    assert result["error"] == "HISTORY_FORMAT_UNSUPPORTED"
    records = json.loads((sessions / "hist-a" / "history.json").read_text(encoding="utf-8"))
    assert records[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_synchronous_queue_writer_keeps_its_existing_behavior(history_host):
    _, sessions = history_host
    session_history.append_history_record(session_id="hist-a", request_id="sync-original", channel_id="web",
                                          role="assistant", content="platform:sync", timestamp=123.0)
    await asyncio.to_thread(session_history.flush_history_writes)
    assert _rows(sessions)[0]["content"] == "platform:sync"


@pytest.mark.asyncio
async def test_existing_history_append_rpc_uses_policy_and_returns_committed_data(history_host):
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    class WireSocket:
        def __init__(self):
            self.frames = []

        async def send(self, value):
            self.frames.append(json.loads(value))

    _, sessions = history_host
    ws = WireSocket()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = AgentRequest(request_id="rpc-history", channel_id="web", session_id="hist-a", req_method=ReqMethod.HISTORY_APPEND_RECORD,
                           params={"request_id": "stable-rpc", "role": "assistant", "content": "platform:rpc", "event_type": "chat.final"})
    await server._handle_history_append_record(ws, request, asyncio.Lock())
    assert parse_agent_server_wire_unary(ws.frames[-1]).ok
    await server._handle_history_append_record(ws, request, asyncio.Lock())
    assert parse_agent_server_wire_unary(ws.frames[-1]).ok
    visible = (await _query(history_host))["page"]["messages"]
    assert len(visible) == 1 and visible[0]["content"] == "[checked] platform:rpc"
    request.params = {"role": "assistant", "content": "deny", "request_id": "deny-rpc"}
    await server._handle_history_append_record(ws, request, asyncio.Lock())
    assert not parse_agent_server_wire_unary(ws.frames[-1]).ok
    assert len((await _query(history_host))["page"]["messages"]) == 1
    assert not any(row["content"] == "deny" for row in _rows(sessions))
