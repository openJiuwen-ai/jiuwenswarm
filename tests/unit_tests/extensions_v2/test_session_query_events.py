# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Production assembly, real packages, JSONL/Git and nested query contracts."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess

import pytest
from openjiuwen.core.events import Event, HandlerLevel, PackageMetadata, SessionContext

from jiuwenswarm.common import utils
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.session_queries import SESSION_QUERY_EVENTS, SessionQueryError
from jiuwenswarm.extensions_v2.query_dispatch import current_query_origin
from jiuwenswarm.server.runtime.session import session_history as history
from .management.support import write_package

USER_CODE = '''
from jiuwenswarm.extensions_v2.event_types.session_queries import SessionQueryError
from jiuwenswarm.extensions_v2.query_dispatch import current_query_origin

class Reader:
    def __init__(self):
        self.calls = []

    def register(self, registrar):
        registrar.register_event_handler("test.query", self.read, {})
        for name in ("cwd", "root", "model", "repo", "messages", "turns", "usage"):
            registrar.register_event_handler("session." + name, self.observe, {})

    async def observe(self, event, ops, next_handler):
        origin = current_query_origin()
        self.calls.append((event.event_type, dict(event.event_input),
                           None if origin is None else origin.package))
        return await next_handler(event)

    async def read(self, event, ops, next_handler):
        request = event.event_input
        try:
            names = (request['method'],) if 'method' in request else (
                "cwd", "root", "model", "repo", "messages", "turns", "usage")
            values = {}
            for name in names:
                values[name] = await getattr(ops.session, name)(**request.get('arguments', {}))
            return {"values": values, "calls": list(self.calls)}
        except SessionQueryError as error:
            return {"error": error.code, "reason": getattr(error, 'reason', None)}
        except (TypeError, RuntimeError) as error:
            return {"error": type(error).__name__}

def create_extension():
    return Reader()
'''


def _git(directory, *arguments):
    result = subprocess.run(["git", *arguments], cwd=directory, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _window(sid="session-a", tokens=120, estimated=False):
    return {"schema_version": "session-usage.v1", "session_id": sid, "source": "host",
            "context": {"request_id": "r2", "phase": "post_call", "timestamp": "2026-10-31T02:00:00Z",
                        "sequence": 2, "context_window": {"input_tokens": tokens, "limit_tokens": 1000},
                        "measurement": {"estimated": estimated, "authoritative_total": not estimated},
                        "parts": {"messages": {"tokens": 90, "estimated": True}}}, "turn": None}


@pytest.fixture
async def query_host(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: sessions)
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    (project / "README.md").write_text("query fixture", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "-c", "user.name=Query Tester", "-c", "user.email=query@example.invalid", "commit", "-qm", "initial")
    directory = sessions / "session-a"
    directory.mkdir()
    records = [
        {"id": "u1", "request_id": "r1", "role": "user", "content": "first", "timestamp": 1},
        {"id": "a1", "request_id": "r1", "role": "assistant", "content": "answer", "event_type": "chat.final"},
        {"id": "u2", "request_id": "r2", "role": "user", "content": "second", "timestamp": 2},
        {"id": "usage", "role": "assistant", "event_type": "chat.usage_summary", "content": "not a message"},
    ]
    (directory / "history.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    instance = await assembly.install_extensions()
    assert instance is not None

    async def original_core(event, ops, next_handler):
        return None

    instance.bus.register_event_type("test.query", lambda value, pattern: True, original_core)
    package = write_package(tmp_path / "source", package_id="query.reader", module_source=USER_CODE)
    reports = await instance.manager.install([package])
    assert reports[0].status == "SUCCESS"
    context = SessionContext("session-a", str(project), {
        "project_root": str(tmp_path),
        "session_model_info": {"selection": None, "model_name": "query-model", "provider": "fixture",
                               "source": "runtime", "activated": True},
        "session_usage_info": _window(),
    })
    try:
        yield instance, context, sessions, project
    finally:
        await assembly.shutdown_extensions()
        assert history.flush_pending_writes()


async def _read(host, context, method=None, arguments=None):
    request = {} if method is None else {"method": method}
    if arguments is not None:
        request["arguments"] = arguments
    return await host.bus.trigger(Event("test.query", request), context)


@pytest.mark.asyncio
async def test_real_package_queries_all_seven_through_same_name_events(query_host):
    host, context, sessions, project = query_host
    result = await _read(host, context)
    values = result["values"]
    assert values["cwd"] == str(project) and values["root"] == str(project.parent)
    assert values["model"] == "query-model"
    assert values["repo"] == {"root": str(project), "remote": None, "internal": False, "name": None}
    assert values["messages"] == [
        {"role": "user", "text": "first", "toolUses": []},
        {"role": "assistant", "text": "answer", "toolUses": []},
        {"role": "user", "text": "second", "toolUses": []},
    ]
    assert values["turns"] == 2
    assert values["usage"] == {"context": {"tokens": 120, "window": 1000, "percent": 12}, "rateLimits": []}
    assert [name for name, arguments, origin in result["calls"]] == list(SESSION_QUERY_EVENTS)
    assert all(arguments == {} and origin == "query.reader" for name, arguments, origin in result["calls"])


@pytest.mark.asyncio
async def test_query_user_builtin_core_order_and_separate_caller_owner(query_host, monkeypatch):
    from jiuwenswarm.extensions_v2.event_handlers import session_queries
    host, context, _, _ = query_host
    trace = []
    original = session_queries.read_session_query

    async def core(*args):
        trace.append("core")
        return await original(*args)

    async def user(event, ops, next_handler):
        assert current_query_origin().package == "query.reader"
        assert ops.package_metadata.package_id == "query.audit"
        assert callable(ops.session.model) and callable(ops.session.messages)
        trace.append("user-before")
        result = await next_handler(event)
        trace.append("user-after")
        return result

    async def builtin(event, ops, next_handler):
        trace.append("builtin-before")
        result = await next_handler(event)
        trace.append("builtin-after")
        return result

    monkeypatch.setattr(session_queries, "read_session_query", core)
    host.bus.register_event_handler("session.cwd", user, {}, package_metadata=PackageMetadata("query.audit"))
    host.bus.register_event_handler("session.cwd", builtin, {}, HandlerLevel.BUILTIN)
    assert (await _read(host, context, "cwd"))["values"]["cwd"] == context.cwd
    assert trace == ["user-before", "builtin-before", "core", "builtin-after", "user-after"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value", [
    ("cwd", None), ("root", None), ("model", "virtual-model"), ("repo", None),
    ("messages", []), ("turns", 99), ("usage", {"context": {}, "rateLimits": []}),
])
async def test_each_query_can_short_circuit_without_calling_core(query_host, monkeypatch, name, value):
    from jiuwenswarm.extensions_v2.event_handlers import session_queries
    host, context, _, _ = query_host

    async def unexpected_core(*args):
        pytest.fail("a valid short circuit must not execute the raw reader")

    async def replace(event, ops, next_handler):
        return {"value": copy.deepcopy(value)}

    monkeypatch.setattr(session_queries, "read_session_query", unexpected_core)
    host.bus.register_event_handler("session." + name, replace, {})
    assert (await _read(host, context, name))["values"][name] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [name.split(".")[1] for name in SESSION_QUERY_EVENTS])
async def test_each_query_has_explicit_denial_visible_to_ops_caller(query_host, name):
    host, context, _, _ = query_host

    async def deny(event, ops, next_handler):
        return {"deny": "ACCOUNT_POLICY"}

    host.bus.register_event_handler("session." + name, deny, {})
    assert await _read(host, context, name) == {"error": "DENIED", "reason": "ACCOUNT_POLICY"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["throw", "bare", "ambiguous", "bad-type", "none"])
async def test_handler_failure_is_isolated_and_is_not_denial(query_host, kind):
    host, context, _, _ = query_host

    async def invalid(event, ops, next_handler):
        if kind == "throw":
            raise ValueError("intentional query Hook failure")
        return {"bare": context.cwd, "ambiguous": {"value": context.cwd, "deny": "no"},
                "bad-type": {"value": 42}, "none": None}[kind]

    host.bus.register_event_handler("session.cwd", invalid, {})
    assert (await _read(host, context, "cwd"))["values"]["cwd"] == context.cwd


@pytest.mark.asyncio
@pytest.mark.parametrize("method,arguments", [
    ("cwd", {"session_id": "other"}), ("root", {"path": "elsewhere"}),
    ("model", {"event_type": "session.cwd"}), ("messages", {"limit": 5}),
    ("messages", {"cursor": "fake"}), ("usage", {"breakdown": True}),
    ("usage", {"columns": True}), ("usage", {"columns": 0}),
])
async def test_public_call_cannot_override_routing_or_use_old_parameter_contract(query_host, method, arguments):
    host, context, _, _ = query_host
    assert (await _read(host, context, method, arguments))["error"] == "TypeError"


@pytest.mark.asyncio
async def test_usage_input_rewrite_and_partial_summary_are_observable(query_host):
    host, context, _, _ = query_host

    async def summary(event, ops, next_handler):
        return await next_handler(Event(event.event_type, {"breakdown": "summary", "columns": 60}))

    host.bus.register_event_handler("session.usage", summary, {})
    value = (await _read(host, context, "usage"))["values"]["usage"]
    assert value["context"]["breakdown"]["totalTokens"] == 90
    assert value["context"]["breakdown"]["categories"][0]["kind"] == "used"
    assert "cost" not in value and value["rateLimits"] == []
    assert "gridRows" not in value["context"]["breakdown"]


@pytest.mark.asyncio
async def test_full_counting_is_not_faked_by_a_read_only_query(query_host):
    host, context, _, _ = query_host
    assert (await _read(host, context, "usage", {"breakdown": "full"}))["error"] == "FULL_BREAKDOWN_UNAVAILABLE"


@pytest.mark.asyncio
async def test_query_result_view_does_not_rebind_real_repo_or_model(query_host):
    host, context, _, project = query_host

    async def virtual_cwd(event, ops, next_handler):
        return {"value": str(project / "display-only")}

    host.bus.register_event_handler("session.cwd", virtual_cwd, {})
    values = (await _read(host, context))["values"]
    assert values["cwd"].endswith("display-only")
    assert values["repo"]["root"] == str(project)
    assert context.cwd == str(project) and values["model"] == "query-model"


@pytest.mark.asyncio
async def test_same_query_and_indirect_cycle_are_bounded(query_host):
    host, context, _, _ = query_host
    errors = []

    async def cwd(event, ops, next_handler):
        await ops.session.root()
        return await next_handler(event)

    async def root(event, ops, next_handler):
        try:
            await ops.session.cwd()
        except SessionQueryError as error:
            errors.append(error.code)
        return await next_handler(event)

    host.bus.register_event_handler("session.cwd", cwd, {})
    host.bus.register_event_handler("session.root", root, {})
    assert (await _read(host, context, "cwd"))["values"]["cwd"] == context.cwd
    assert errors == ["QUERY_REENTRANT"]


@pytest.mark.asyncio
async def test_cancelled_query_releases_scope_and_does_not_become_success(query_host):
    host, context, _, _ = query_host
    entered = asyncio.Event()

    async def slow(event, ops, next_handler):
        entered.set()
        await asyncio.Event().wait()

    handler = host.bus.register_event_handler("session.cwd", slow, {})
    task = asyncio.create_task(_read(host, context, "cwd"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    host.bus.unregister_event_handler(handler)
    assert (await _read(host, context, "cwd"))["values"]["cwd"] == context.cwd
    assert current_query_origin() is None


@pytest.mark.asyncio
async def test_inherited_completed_query_frame_does_not_poison_child_task(query_host):
    host, context, _, _ = query_host
    release = asyncio.Event()
    children = []

    async def deferred(ops):
        await release.wait()
        return await ops.session.cwd()

    async def spawn_once(event, ops, next_handler):
        if not children:
            children.append(asyncio.create_task(deferred(ops)))
        return await next_handler(event)

    host.bus.register_event_handler("session.cwd", spawn_once, {})
    await _read(host, context, "cwd")
    release.set()
    assert await children[0] == context.cwd


@pytest.mark.asyncio
async def test_two_sessions_keep_model_and_query_context_separate(query_host):
    host, context, _, _ = query_host
    other = SessionContext("session-b", context.cwd, {"session_model_info": {
        "selection": None, "model_name": "other-model", "provider": "fixture", "source": "runtime", "activated": True}})
    first, second = await asyncio.gather(_read(host, context, "model"), _read(host, other, "model"))
    assert first["values"]["model"] == "query-model"
    assert second["values"]["model"] == "other-model"


@pytest.mark.asyncio
async def test_unknown_bound_usage_never_falls_back_or_invents_zero(query_host):
    host, context, _, _ = query_host
    unknown = SessionContext("session-a", metadata={"session_usage_info": None})
    assert (await _read(host, unknown, "usage"))["values"]["usage"] == {"context": {}, "rateLimits": []}
    estimated = SessionContext("session-a", metadata={"session_usage_info": _window(estimated=True)})
    assert (await _read(host, estimated, "usage"))["values"]["usage"] == {"context": {"window": 1000}, "rateLimits": []}


@pytest.mark.asyncio
async def test_repo_uses_push_remote_without_returning_credentials(query_host):
    host, context, _, project = query_host
    _git(project, "config", "remote.origin.url", "https://fetch.example.invalid/owner/repo.git")
    _git(project, "config", "remote.origin.pushurl", "https://name:secret@push.example.invalid/owner/repo.git?token=secret")
    repo = (await _read(host, context, "repo"))["values"]["repo"]
    assert repo["remote"] == "https://push.example.invalid/owner/repo.git"
    assert "secret" not in json.dumps(repo)


@pytest.mark.asyncio
async def test_linked_worktree_repo_returns_main_root_like_claude(query_host):
    host, context, _, project = query_host
    linked = project.parent / "linked"
    _git(project, "worktree", "add", "-q", "-b", "linked-query", str(linked))
    try:
        repo = (await _read(host, SessionContext("linked-session", str(linked)), "repo"))["values"]["repo"]
        assert repo["root"] == str(project)
    finally:
        _git(project, "worktree", "remove", str(linked))


@pytest.mark.asyncio
async def test_messages_pair_real_tool_calls_and_results_without_ui_rows(query_host):
    host, context, sessions, _ = query_host
    rows = [
        {"role": "assistant", "event_type": "chat.tool_call", "request_id": "r3", "content": "",
         "tool_calls": [{"id": "tool-1", "function": {"name": "Read", "arguments": '{"path":"README.md"}'}}]},
        {"role": "assistant", "event_type": "chat.tool_result", "request_id": "r3", "content": "",
         "tool_call_id": "tool-1", "rendered_result": "contents", "result": {"ok": True}},
        {"role": "assistant", "event_type": "chat.notice", "content": "ui log"},
    ]
    with (sessions / "session-a" / "history.jsonl").open("a", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    messages = (await _read(host, context, "messages"))["values"]["messages"]
    call = messages[-2]["toolUses"][0]
    assert call == {"tool_use_id": "tool-1", "tool": "Read", "input": {"path": "README.md"},
                    "text": "contents", "result": {"ok": True}}
    assert messages[-1]["toolResults"] == [{"tool_use_id": "tool-1", "text": "contents", "isError": False,
                                           "result": {"ok": True}}]
    assert all(message["text"] != "ui log" for message in messages)


@pytest.mark.asyncio
async def test_messages_cap_is_4096_in_chronological_order(query_host):
    host, context, sessions, _ = query_host
    rows = [{"role": "user", "content": str(index), "request_id": str(index)} for index in range(4100)]
    (sessions / "session-a" / "history.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    messages = (await _read(host, context, "messages"))["values"]["messages"]
    assert len(messages) == 4096
    assert messages[0]["text"] == "4" and messages[-1]["text"] == "4099"

@pytest.mark.asyncio
async def test_unload_removes_query_user_hooks_but_leaves_platform_queries(query_host):
    host, context, _, _ = query_host
    assert (await host.manager.uninstall("query.reader")).status == "SUCCESS"

    async def host_query(event, ops, next_handler):
        return await ops.session.cwd()

    host.bus.register_event_handler("test.query", host_query, {})
    assert await host.bus.trigger(Event("test.query", {}), context) == context.cwd
