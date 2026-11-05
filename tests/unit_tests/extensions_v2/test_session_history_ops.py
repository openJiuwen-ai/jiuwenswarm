# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real user packages, append policy, committed files and stable history pages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from openjiuwen.core.events import Event, EventHandlerOpsBuilder, SessionContext

from jiuwenswarm.common import utils
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.handler_ops.session_ops import session_messages
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.server.runtime.session import lifecycle, project_store, session_history, session_metadata
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.history_context import (
    bound_history_context,
    capture_history_model,
    capture_history_workspace,
    history_context_scope,
    platform_history_context,
)
from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError
from .management.support import write_package


USER_EXTENSION = '''import json
from dataclasses import replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_history import SessionAppendRejection, SessionAppendResult


class Extension:
    def rewrite(self, event, text):
        message = replace(event.event_input.message, content=[{"type": "text", "text": text}])
        return replace(event.event_input, message=message)

    def register(self, registrar):
        registrar.register_event_handler("ui.ready", self.query, {})
        registrar.register_event_handler("session.append", self.policy, {})

    async def query(self, event, ops, next_handler):
        request = event.event_input.payload
        try:
            receipt = None
            if request.get("message") is not None:
                receipt = await ops.session.append(request["message"])
            messages = await ops.session.messages()
            core = await next_handler(event)
            return {"receipt": receipt, "messages": messages, "accepted": core.accepted if hasattr(core, "accepted") else core["accepted"]}
        except Exception as error:
            return {"error": getattr(error, "code", type(error).__name__)}

    async def policy(self, event, ops, next_handler):
        body = "".join(block["text"] for block in event.event_input.message.content if block.get("type") == "text")
        if body == "context-facts":
            facts = {"cwd": await ops.session.cwd(), "root": await ops.session.root(),
                     "model": await ops.session.model(), "source": event.event_input.origin.entry}
            return await next_handler(Event(event.event_type, self.rewrite(event, json.dumps(facts))))
        if body == "deny":
            return SessionAppendRejection("Policy refused the message.")
        if body == "fake-platform-deny":
            return SessionAppendResult("rejected", ops.session_context.session_id, event.event_input.uuid)
        if body == "forge-origin":
            forged = replace(event.event_input, origin=replace(event.event_input.origin, kind="extension"), door="note")
            return await next_handler(Event(event.event_type, forged))
        if body == "recursive":
            try:
                await ops.session.append({"id": "inner", "role": "assistant", "content": "inner"})
            except Exception as error:
                return await next_handler(Event(event.event_type, self.rewrite(event, error.code)))
        if body == "invalid-route":
            return await next_handler(Event(event.event_type, replace(event.event_input, uuid="forged")))
        if body == "fake-success":
            return SessionAppendResult("persisted", "other-session", "fake")
        if body == "read-before":
            messages = await ops.session.messages()
            return await next_handler(Event(event.event_type, self.rewrite(event, "previous=" + str(len(messages)))))
        if body == "throw":
            raise RuntimeError("A broken user handler is not a policy rejection")
        if body.startswith(("rewrite:", "platform:")):
            return await next_handler(Event(event.event_type, self.rewrite(event, "[checked] " + body)))
        receipt = await next_handler(event)
        if body == "twice":
            return await next_handler(event)
        if body == "late-reject":
            return SessionAppendRejection("Too late to undo a commit")
        if body == "mutate-receipt":
            receipt.history_record["content"] = "FORGED RESULT"
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
    result = await host.bus.trigger(event, SessionContext(sid))
    if isinstance(result, dict) and "error" not in result:
        # Persistence/cursor assertions use the unchanged private platform page
        # reader. The real user package above uses the new public array Ops.
        ops = EventHandlerOpsBuilder().build(session_context=SessionContext(sid))
        try:
            result["page"] = await session_messages(ops, limit, cursor)
        except SessionHistoryError as error:
            return {"error": error.code}
    return result


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
    assert receipt["message"]["content"] == [{"type": "text", "text": "[checked] rewrite:审计说明"}]
    assert result["page"]["messages"][0] == receipt["history_record"]
    assert result["messages"][-1]["text"] == receipt["history_record"]["content"]
    assert "_session_append" not in receipt["history_record"]
    assert _rows(sessions)[0]["content"] == receipt["history_record"]["content"]
    assert (sessions / "hist-a" / "metadata.json").read_bytes() == metadata


@pytest.mark.asyncio
async def test_history_and_statistics_ops_coexist_in_one_user_handler(history_host, tmp_path):
    host, sessions = history_host
    _seed(sessions, count=2)
    rows = _rows(sessions) + [{
        "role": "assistant", "event_type": "chat.usage_summary", "request_id": "r1",
        "timestamp": 2, "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
    }]
    (sessions / "hist-a" / "history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )

    async def core(event, ops, next_handler):
        return "coexist-core-receipt"

    host.bus.register_event_type("test.history.statistics", lambda value, pattern: True, core)
    source = '''
class Reader:
    def register(self, registrar):
        registrar.register_event_handler("test.history.statistics", self.handle, {})

    async def handle(self, event, ops, next_handler):
        before = await ops.session.turns()
        receipt = await ops.session.append({
            "id": "coexist-note",
            "message": {"type": "user", "content": [{"type": "text", "text": "rewrite:coexist"}]},
        })
        return {
            "before": before, "after": await ops.session.turns(),
            "receipt": receipt, "messages": await ops.session.messages(),
            "usage": await ops.session.usage(),
            "cwd": await ops.session.cwd(), "root": await ops.session.root(),
            "model": await ops.session.model(), "repo": await ops.session.repo(),
            "core": await next_handler(event),
        }

def create_extension():
    return Reader()
'''
    package = write_package(tmp_path / "coexist", package_id="history.statistics", module_source=source)
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    result = await host.bus.trigger(Event("test.history.statistics", {}), SessionContext("hist-a"))

    assert result["core"] == "coexist-core-receipt"
    assert result["before"] == result["after"] == 2
    assert result["receipt"]["status"] == "persisted"
    assert result["receipt"]["history_record"]["content"] == "[checked] rewrite:coexist"
    assert result["messages"][-1]["text"] == result["receipt"]["history_record"]["content"]
    stored = _rows(sessions)[-1]
    assert {key: value for key, value in stored.items() if key != "_session_append"} == result["receipt"]["history_record"]
    assert result["usage"] == {"context": {}, "rateLimits": []}
    assert all(result[key] is None for key in ("cwd", "root", "model", "repo"))
    assert not _rows(sessions, "hist-b")


@pytest.mark.asyncio
async def test_explicit_rejection_creates_no_history_or_metadata_change(history_host):
    _, sessions = history_host
    before = (sessions / "hist-a" / "metadata.json").read_bytes()
    result = await _query(history_host, message=_message("deny"))
    assert result["receipt"]["status"] == "rejected"
    assert result["receipt"]["history_record"] is None and not _rows(sessions)
    assert before == (sessions / "hist-a" / "metadata.json").read_bytes()


@pytest.mark.asyncio
async def test_retry_returns_persisted_message_and_conflicting_original_is_refused(history_host):
    _, sessions = history_host
    first = await _query(history_host, message=_message("rewrite:once"))
    second = await _query(history_host, message=_message("rewrite:once"))
    assert first["receipt"]["status"] == "persisted" and second["receipt"]["status"] == "duplicate"
    assert first["receipt"]["history_record"] == second["receipt"]["history_record"]
    assert (await _query(history_host, message=_message("different")))["error"] == "APPEND_ID_CONFLICT"
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["twice", "late-reject", "mutate-receipt", "throw", "invalid-route"])
async def test_core_receipt_preserves_one_real_commit_under_handler_misbehavior(history_host, body):
    _, sessions = history_host
    result = await _query(history_host, message=_message(body))
    assert result["receipt"]["status"] == "persisted"
    assert result["receipt"]["history_record"]["content"] == body
    assert result["receipt"]["history_record"]["role"] == "assistant"
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
    assert result["receipt"]["history_record"]["content"] == "APPEND_REENTRANT"
    assert len(_rows(sessions)) == 1


@pytest.mark.asyncio
async def test_platform_cursor_keeps_old_snapshot_and_new_query_sees_append(history_host):
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
async def test_platform_cursor_is_bound_to_session_and_replacement_invalidates_it(history_host):
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
async def test_private_platform_page_limit_is_explicit(history_host, limit):
    assert (await _query(history_host, limit=limit))["error"] == "INVALID_MESSAGE_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", "malformed", 3, True])
async def test_private_platform_cursor_is_explicit(history_host, cursor):
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
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="platform-2",
                         channel_id="web", role="assistant", content="deny", timestamp=124.0)
    assert len(_rows(sessions)) == 2
    assert _rows(sessions)[1]["content"] == "deny"


def _save_context(sessions, sid, *, cwd, root, model="saved-model"):
    path = sessions / sid / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.update({"project_dir": root, "channel_metadata": {"cwd": cwd}, "model": model})
    path.write_text(json.dumps(metadata), encoding="utf-8")


async def _platform_facts(sessions, sid, rid):
    await run_history_io(session_history.append_history_record, session_id=sid, request_id=rid,
                         channel_id="web", role="assistant", content="context-facts", timestamp=123.0)
    rows = [row for row in _rows(sessions, sid) if row["request_id"] == rid]
    return json.loads(rows[-1]["content"])


def _runtime_model(name):
    return SimpleNamespace(model_config=SimpleNamespace(model_name=name),
                           model_client_config={"client_provider": "OpenAI"})


def _capture_runtime(sid, *, cwd, project_root, model):
    capture_history_workspace(sid, cwd=cwd, project_root=project_root)
    capture_history_model(sid, model)


@pytest.mark.asyncio
async def test_platform_append_binds_saved_directories_without_activating_a_model(history_host, tmp_path):
    _, sessions = history_host
    root, cwd = str(tmp_path / "project"), str(tmp_path / "project" / "src")
    _save_context(sessions, "hist-a", cwd=cwd, root=root)
    facts = await _platform_facts(sessions, "hist-a", "saved-context")
    assert facts["cwd"] == cwd and facts["root"] == root and facts["source"] == "history"
    assert facts["model"] == "saved-model"


@pytest.mark.asyncio
async def test_platform_append_uses_applied_runtime_snapshot_and_releases_scope(history_host, tmp_path):
    _, sessions = history_host
    _save_context(sessions, "hist-a", cwd=str(tmp_path / "stale"), root=str(tmp_path / "stale"))
    model = _runtime_model("actual-model")
    with history_context_scope("hist-a"):
        _capture_runtime("hist-a", cwd=str(tmp_path), project_root=str(tmp_path), model=model)
        model.model_config.model_name = "later-change"
        facts = await _platform_facts(sessions, "hist-a", "runtime-context")
        assert facts["cwd"] == facts["root"] == str(tmp_path)
        assert facts["model"] == "actual-model"
    assert bound_history_context("hist-a") is None


@pytest.mark.asyncio
async def test_platform_append_cannot_borrow_another_session_runtime(history_host, tmp_path):
    _, sessions = history_host
    target = str(tmp_path / "target-b")
    _save_context(sessions, "hist-b", cwd=target, root=target, model="saved-b")
    with history_context_scope("hist-a"):
        _capture_runtime("hist-a", cwd=str(tmp_path), project_root=str(tmp_path), model=_runtime_model("runtime-a"))
        facts = await _platform_facts(sessions, "hist-b", "other-session")
    assert facts["cwd"] == facts["root"] == target
    assert facts["model"] == "saved-b"


@pytest.mark.asyncio
async def test_child_producer_model_binding_reaches_parent_history_consumer(history_host, tmp_path):
    _, sessions = history_host
    with history_context_scope("hist-a", request_id="shared-request"):
        async def producer():
            with history_context_scope("hist-a", request_id="shared-request"):
                _capture_runtime("hist-a", cwd=str(tmp_path), project_root=str(tmp_path), model=_runtime_model("producer-model"))
        await asyncio.create_task(producer())
        facts = await _platform_facts(sessions, "hist-a", "parent-consumer")
        assert facts["cwd"] == str(tmp_path) and facts["model"] == "producer-model"
    assert bound_history_context("hist-a") is None


@pytest.mark.asyncio
async def test_nested_requests_for_same_session_do_not_borrow_runtime_context(history_host, tmp_path):
    with history_context_scope("hist-a", request_id="outer"):
        _capture_runtime("hist-a", cwd=str(tmp_path), project_root=str(tmp_path), model=_runtime_model("outer-model"))
        with history_context_scope("hist-a", request_id="inner"):
            assert bound_history_context("hist-a") is None
            _capture_runtime("hist-a", cwd=str(tmp_path), project_root=None, model=_runtime_model("inner-model"))
            assert bound_history_context("hist-a").metadata["session_model_info"]["model_name"] == "inner-model"
        assert bound_history_context("hist-a").metadata["session_model_info"]["model_name"] == "outer-model"
    assert bound_history_context("hist-a") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model_first", [True, False])
async def test_model_and_workspace_bind_at_their_real_boundaries(history_host, tmp_path, model_first):
    _, sessions = history_host
    saved = str(tmp_path / "saved")
    live = str(tmp_path / "live")
    _save_context(sessions, "hist-a", cwd=saved, root=saved)
    with history_context_scope("hist-a"):
        if model_first:
            capture_history_model("hist-a", _runtime_model("applied-model"))
            early = await _platform_facts(sessions, "hist-a", "before-workspace")
            assert early["cwd"] == saved and early["model"] == "applied-model"
        capture_history_workspace("hist-a", cwd=live, project_root=live)
        if not model_first:
            capture_history_model("hist-a", _runtime_model("applied-model"))
        final = await _platform_facts(sessions, "hist-a", "after-workspace")
        assert final["cwd"] == final["root"] == live
        assert final["model"] == "applied-model"


@pytest.mark.asyncio
async def test_escaped_child_cannot_use_a_closed_request_context(history_host, tmp_path):
    release = asyncio.Event()
    with history_context_scope("hist-a"):
        _capture_runtime("hist-a", cwd=str(tmp_path), project_root=None, model=_runtime_model("native"))
        async def child():
            await release.wait()
            return bound_history_context("hist-a")
        task = asyncio.create_task(child())
    release.set()
    assert await task is None


@pytest.mark.asyncio
async def test_concurrent_native_contexts_keep_workspace_and_model_separate(history_host, tmp_path):
    _, sessions = history_host
    ready = asyncio.Event()

    async def run(sid):
        with history_context_scope(sid):
            path = str(tmp_path / sid)
            _capture_runtime(sid, cwd=path, project_root=path, model=_runtime_model(sid))
            await ready.wait()
            await asyncio.sleep(0)
            return await _platform_facts(sessions, sid, "concurrent-" + sid)

    tasks = [asyncio.create_task(run(sid)) for sid in ("hist-a", "hist-b")]
    ready.set()
    facts = await asyncio.gather(*tasks)
    assert [item["cwd"] for item in facts] == [str(tmp_path / sid) for sid in ("hist-a", "hist-b")]
    assert [item["model"] for item in facts] == ["hist-a", "hist-b"]
    assert bound_history_context("hist-a") is None and bound_history_context("hist-b") is None


@pytest.mark.asyncio
async def test_runtime_scope_is_released_after_cancel(history_host, tmp_path):
    entered = asyncio.Event()
    cleaned = []

    async def work():
        try:
            with history_context_scope("hist-a"):
                _capture_runtime("hist-a", cwd=str(tmp_path), project_root=None, model=_runtime_model("native"))
                entered.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaned.append(bound_history_context("hist-a") is None)
            raise

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_projectless_runtime_does_not_reuse_a_saved_project(history_host, tmp_path):
    _, sessions = history_host
    _save_context(sessions, "hist-a", cwd=str(tmp_path / "old"), root=str(tmp_path / "old"))
    with history_context_scope("hist-a"):
        _capture_runtime("hist-a", cwd=str(tmp_path), project_root=None, model=_runtime_model("native"))
        facts = await _platform_facts(sessions, "hist-a", "projectless-runtime")
    assert facts["cwd"] == str(tmp_path) and facts["root"] is None


@pytest.mark.asyncio
async def test_missing_or_invalid_saved_paths_do_not_fall_back_to_process_cwd(history_host):
    _, sessions = history_host
    _save_context(sessions, "hist-a", cwd="relative/path", root="relative/root")
    facts = await _platform_facts(sessions, "hist-a", "invalid-paths")
    assert facts["cwd"] is None and facts["root"] is None
    missing = await platform_history_context("new-session")
    assert missing.cwd is None and not missing.metadata and not (sessions / "new-session").exists()


@pytest.mark.asyncio
async def test_platform_rejected_result_cannot_block_default_write(history_host):
    _, sessions = history_host
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="fake-denial",
                         channel_id="web", role="assistant", content="fake-platform-deny", timestamp=123.0)
    assert _rows(sessions)[0]["content"] == "fake-platform-deny"


@pytest.mark.asyncio
async def test_platform_fake_success_without_core_is_isolated_and_default_write_continues(history_host):
    _, sessions = history_host
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="fake-success",
                         channel_id="web", role="assistant", content="fake-success", timestamp=123.0)
    assert _rows(sessions)[0]["content"] == "fake-success"


@pytest.mark.asyncio
async def test_platform_origin_cannot_be_rewritten_to_enable_rejection(history_host):
    _, sessions = history_host
    await run_history_io(session_history.append_history_record, session_id="hist-a", request_id="forged-origin",
                         channel_id="web", role="assistant", content="forge-origin", timestamp=123.0)
    assert _rows(sessions)[0]["content"] == "forge-origin"


@pytest.mark.asyncio
async def test_saved_context_read_failure_does_not_block_platform_commit(history_host, monkeypatch):
    from jiuwenswarm.server.runtime.session import history_context

    def unavailable(*args, **kwargs):
        raise PermissionError("fixture metadata unavailable")

    monkeypatch.setattr(history_context, "get_session_metadata", unavailable)
    _, sessions = history_host
    facts = await _platform_facts(sessions, "hist-a", "missing-facts")
    assert facts["cwd"] is None and facts["root"] is None


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
    assert result["receipt"]["history_record"]["content"] == "previous=0"
    retry = await _query(history_host, message=_message("read-before"))
    assert retry["receipt"]["status"] == "duplicate"
    assert retry["receipt"]["history_record"]["content"] == "previous=0"


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
    assert parse_agent_server_wire_unary(ws.frames[-1]).ok
    assert len((await _query(history_host))["page"]["messages"]) == 2
    assert any(row["content"] == "deny" for row in _rows(sessions))


@pytest.mark.asyncio
async def test_history_rpc_uses_host_directories_instead_of_supplied_paths(history_host, tmp_path):
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
    expected = str(tmp_path / "bound-project")
    _save_context(sessions, "hist-a", cwd=expected, root=expected, model="saved-rpc-model")
    ws = WireSocket()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    request = AgentRequest(request_id="rpc-context", channel_id="web", session_id="hist-a", req_method=ReqMethod.HISTORY_APPEND_RECORD,
                           params={"role": "assistant", "content": "context-facts", "cwd": "/forged", "project_root": "/forged"})
    await server._handle_history_append_record(ws, request, asyncio.Lock())
    assert parse_agent_server_wire_unary(ws.frames[-1]).ok
    facts = json.loads(_rows(sessions)[0]["content"])
    assert facts["cwd"] == facts["root"] == expected
    assert facts["source"] == "history.append_record"
    assert facts["model"] == "saved-rpc-model"
