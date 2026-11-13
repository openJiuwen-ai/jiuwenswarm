# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front query liveness, tenant propagation and cancellation admission."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import gc
import json
import threading
import time

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.server.control.repositories import project_repository, session_repository


async def _until(predicate, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _request(method: ReqMethod) -> AgentRequest:
    return AgentRequest(request_id="query", channel_id="web", user_id="owner",
                        req_method=method, params={"project_id": "default"})


@pytest.mark.asyncio
@pytest.mark.parametrize("method, loader", [
    (ReqMethod.PROJECT_LIST, "load_project_list"),
    (ReqMethod.PROJECT_INFO, "load_project_info"),
    (ReqMethod.PROJECT_PINNED_SESSIONS, "load_pinned_sessions"),
    (ReqMethod.PROJECT_GET_SESSIONS, "load_project_sessions"),
    (ReqMethod.PROJECT_GET_CRON_SESSIONS, "load_project_cron_sessions"),
    (ReqMethod.PROJECT_CRON_RESOLVE_BINDING, "resolve_cron_binding"),
])
async def test_project_scan_leaves_front_loop_running(monkeypatch, method, loader):
    started = threading.Event()
    release = threading.Event()
    threads = []
    payload = {"sessions": [], "total": 0}

    def scan(*_args):
        threads.append(threading.get_ident())
        started.set()
        release.wait(1)
        return payload if loader == "load_pinned_sessions" else (payload, None, None)

    monkeypatch.setattr(project_repository, loader, scan)
    task = asyncio.create_task(project_repository.handle_project_request(_request(method)))
    try:
        await _until(started.is_set)
        assert not task.done(), "The Front loop could not run while its disk query was pending"
        assert threads == [threads[0]] and threads[0] != threading.get_ident()
    finally:
        release.set()
        response = await task
    assert response.ok and response.payload == payload


@pytest.mark.asyncio
async def test_unary_history_scan_preserves_loop_and_request_context(monkeypatch):
    from jiuwenswarm.server.control import history_service

    started, release = threading.Event(), threading.Event()
    marker = ContextVar("history-query-context", default="unbound")
    observed = []

    def scan(_params):
        observed.append((threading.get_ident(), marker.get()))
        started.set()
        release.wait(2)
        return {"messages": [{"role": "user", "content": "history"}], "page_idx": 1, "total_pages": 1}

    monkeypatch.setattr(history_service, "load_history_query", scan)
    token = marker.set("original-request")
    task = asyncio.create_task(history_service.handle_history_request(_request(ReqMethod.HISTORY_GET)))
    try:
        await _until(started.is_set)
        assert not task.done(), "The history scan blocked the Front control loop"
        assert observed == [(observed[0][0], "original-request")]
        assert observed[0][0] != threading.get_ident()
    finally:
        release.set()
        response = await task
        marker.reset(token)
    assert response.ok and response.payload["messages"][0]["content"] == "history"


@pytest.mark.asyncio
async def test_session_scan_and_projection_leave_front_loop_running(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    scan_threads = []
    projection_threads = []

    def scan(limit=20, offset=0):
        scan_threads.append(threading.get_ident())
        assert (limit, offset) == (7, 3)
        return [{"session_id": "one"}], 11

    def projection(metadata):
        projection_threads.append(threading.get_ident())
        started.set()
        release.wait(1)
        return {"session_id": metadata["session_id"], "title": "visible"}

    monkeypatch.setattr(session_repository, "get_all_sessions_metadata", scan)
    monkeypatch.setattr(session_repository, "to_session_info", projection)
    request = _request(ReqMethod.SESSION_LIST)
    request.params = {"limit": 7, "offset": 3}
    task = asyncio.create_task(session_repository.handle_session_request(request))
    try:
        await _until(started.is_set)
        assert not task.done(), "The Web list projection still blocked the Front loop"
        assert scan_threads == projection_threads
        assert scan_threads[0] != threading.get_ident()
    finally:
        release.set()
        response = await task
    assert response.ok
    assert response.payload == {"sessions": [{"session_id": "one", "title": "visible"}],
                                "total": 11, "limit": 7, "offset": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    ReqMethod.SESSION_GET_METADATA, ReqMethod.SESSION_COLOR_SET, ReqMethod.SESSION_PREVIEW,
])
async def test_session_query_lifecycle_guard_leaves_front_loop_running(monkeypatch, method):
    from jiuwenswarm.server.runtime.session import lifecycle

    started = threading.Event()
    release = threading.Event()
    threads = []

    def guard(_sid):
        threads.append(threading.get_ident())
        started.set()
        release.wait(1)
        raise lifecycle.LifecycleError("OPERATION_IN_PROGRESS", "session is stopping")

    monkeypatch.setattr(lifecycle, "guard", guard)
    request = _request(method)
    request.params = {"session_id": "one"}
    task = asyncio.create_task(session_repository.handle_session_request(request))
    try:
        await _until(started.is_set)
        assert not task.done(), "Lifecycle file access still blocked the Front loop"
        assert threads[0] != threading.get_ident()
    finally:
        release.set()
        response = await task
    assert not response.ok
    assert response.payload["code"] == "OPERATION_IN_PROGRESS"
    assert response.payload["error"] == "session is stopping"


@pytest.mark.asyncio
@pytest.mark.parametrize("method, reader", [
    (ReqMethod.SESSION_COLOR_SET, "get_session_metadata"),
    (ReqMethod.SESSION_PREVIEW, "load_history_records"),
])
async def test_session_color_and_preview_reads_leave_front_loop_running(monkeypatch, method, reader):
    started = threading.Event()
    release = threading.Event()
    threads = []

    def read(*_args, **_kwargs):
        threads.append(threading.get_ident())
        started.set()
        release.wait(1)
        if method == ReqMethod.SESSION_COLOR_SET:
            return {"accent_color": "blue"}
        return [{"role": "user", "content": "kept history"}]

    monkeypatch.setattr(session_repository, reader, read)
    request = _request(method)
    request.params = {"session_id": "one"}
    task = asyncio.create_task(session_repository.handle_session_request(request))
    try:
        await _until(started.is_set)
        assert not task.done(), "A session read still occupied the Front loop"
        assert threads[0] != threading.get_ident()
    finally:
        release.set()
        response = await task
    assert response.ok and response.payload["session_id"] == "one"
    if method == ReqMethod.SESSION_COLOR_SET:
        assert response.payload["accent_color"] == "blue"
    else:
        assert response.payload["preview_messages"] == [
            {"role": "user", "content": "kept history", "event_type": ""},
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, " ", "../bad", 1])
async def test_invalid_metadata_request_does_not_wait_for_query_capacity(session_id):
    from jiuwenswarm.server.control.query_executor import _QUERY_CONCURRENCY, run_control_query

    started = []
    release = threading.Event()

    def read():
        started.append(True)
        release.wait(3)

    occupied = [asyncio.create_task(run_control_query(read)) for _ in range(_QUERY_CONCURRENCY)]
    try:
        await _until(lambda: len(started) == _QUERY_CONCURRENCY)
        request = _request(ReqMethod.SESSION_GET_METADATA)
        request.params = {"session_id": session_id}
        response = await asyncio.wait_for(session_repository.handle_session_request(request), .25)
        assert not response.ok and response.payload["code"] == "BAD_REQUEST"
        assert all(not task.done() for task in occupied)
    finally:
        release.set()
        await asyncio.gather(*occupied)


@pytest.mark.asyncio
async def test_metadata_file_lock_leaves_front_loop_running(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import lifecycle, session_metadata

    sessions = tmp_path / "agent" / "sessions"
    session = sessions / "one"
    session.mkdir(parents=True)
    (session / "metadata.json").write_text(json.dumps({
        "session_id": "one", "title": "visible", "project_id": "project",
        "work_mode": "code", "mode": "agent.code.normal", "channel_id": "web",
        "last_user_message_at": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    assert lifecycle.resource_path("session", "one").is_relative_to(tmp_path)
    locked = threading.Event()
    reading = threading.Event()
    release = threading.Event()
    threads = []
    read = session_repository.get_session_metadata

    def hold_lock():
        with session_metadata._FILE_LOCK:
            locked.set()
            release.wait(5)

    def query_metadata(*args, **kwargs):
        threads.append(threading.get_ident())
        reading.set()
        return read(*args, **kwargs)

    monkeypatch.setattr(session_repository, "get_session_metadata", query_metadata)
    holder = threading.Thread(target=hold_lock)
    holder.start()
    task = None
    try:
        await _until(locked.is_set)
        request = _request(ReqMethod.SESSION_GET_METADATA)
        request.params = {"session_id": "one", "enable_writeback": False}
        task = asyncio.create_task(session_repository.handle_session_request(request))
        await _until(reading.is_set)
        assert not task.done(), "The metadata file lock blocked the Front event loop"
        assert threads == [threads[0]] and threads[0] != threading.get_ident()
    finally:
        release.set()
        holder.join(timeout=5)
        response = await task if task is not None else None
    assert not holder.is_alive()
    assert response.ok and response.payload["session_id"] == "one"
    assert response.payload["title"] == "visible"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [ReqMethod.PROJECT_LIST, ReqMethod.SESSION_GET_METADATA])
async def test_parallel_queries_keep_tenant_and_request_context(monkeypatch, method):
    context = ContextVar("front-query-test", default="unbound")
    previous_tenant = TenantContext.current_tenant_key()

    def scan(*_args, **_kwargs):
        time.sleep(0.02)
        payload = {"tenant": TenantContext.current_tenant_key(), "request": context.get()}
        return payload if method == ReqMethod.SESSION_GET_METADATA else (payload, None, None)

    monkeypatch.setattr(project_repository, "load_project_list", scan)
    monkeypatch.setattr(session_repository, "get_session_metadata", scan)

    async def query(tenant):
        with TenantContext.scoped(tenant):
            token = context.set(tenant + "-request")
            try:
                request = _request(method)
                if method == ReqMethod.SESSION_GET_METADATA:
                    request.params = {"session_id": "one"}
                    return await session_repository.handle_session_request(request)
                return await project_repository.handle_project_request(request)
            finally:
                context.reset(token)

    responses = await asyncio.gather(query("group::bot::alice"), query("group::bot::bob"))
    assert [response.payload for response in responses] == [
        {"tenant": "group::bot::alice", "request": "group::bot::alice-request"},
        {"tenant": "group::bot::bob", "request": "group::bot::bob-request"},
    ]
    assert TenantContext.current_tenant_key() == previous_tenant
    assert context.get() == "unbound"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    ReqMethod.PROJECT_LIST, ReqMethod.SESSION_LIST, ReqMethod.SESSION_GET_METADATA,
])
async def test_router_queries_keep_enterprise_workspace_in_background(monkeypatch, tmp_path, method):
    from jiuwenswarm.common import utils
    from jiuwenswarm.enterprise import plane
    from jiuwenswarm.server.front.admission import ExecutionAdmission
    from jiuwenswarm.server.front.router import MethodRouter
    from jiuwenswarm.server.lifecycle import Readiness

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    # Restore the process-wide edition assembly after exercising this tenant.
    monkeypatch.setattr(plane, "_PLANE", None)
    monkeypatch.setattr(plane, "_BUILD_ATTEMPTED", False)
    assemblies = []
    assemble = plane.try_build_enterprise_plane

    def build_plane(*args, **kwargs):
        assemblies.append(threading.get_ident())
        return assemble(*args, **kwargs)

    monkeypatch.setattr(plane, "try_build_enterprise_plane", build_plane)
    seen = {}

    def scan(*_args, **_kwargs):
        time.sleep(0.02)
        assert plane._PLANE is not None
        tenant = TenantContext.current_tenant_key()
        seen[tenant] = (utils.get_agent_sessions_dir(), threading.get_ident())
        if method == ReqMethod.SESSION_GET_METADATA:
            return {"session_id": "one"}
        return ({"projects": []}, None, None) if method == ReqMethod.PROJECT_LIST else ([], 0)

    monkeypatch.setattr(project_repository, "load_project_list", scan)
    monkeypatch.setattr(session_repository, "get_all_sessions_metadata", scan)
    monkeypatch.setattr(session_repository, "get_session_metadata", scan)
    sent = []

    async def send(_ws, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", send)
    readiness = Readiness()
    router = MethodRouter(readiness, ExecutionAdmission(readiness))

    async def query(user):
        request = _request(method)
        request.request_id = user
        request.user_id = user
        request.metadata = {"routing": {"group_id": "group", "bot_id": "bot"}}
        request.params["session_id"] = "one"
        await router.dispatch(object(), request, asyncio.Lock())

    await asyncio.gather(query("alice"), query("bob"))
    assert len(sent) == 2
    assert assemblies == [threading.get_ident()]
    for user in ("alice", "bob"):
        tenant = utils.build_tenant_key("group", "bot", user)
        path, thread = seen[tenant]
        assert tenant in str(path) and path.is_relative_to(tmp_path)
        assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_cancelled_requests_keep_slots_until_native_queries_finish():
    from jiuwenswarm.server.control.query_executor import _QUERY_CONCURRENCY, run_control_query

    release = threading.Event()
    mutex = threading.Lock()
    active = 0
    peak = 0
    calls = 0

    def scan():
        nonlocal active, peak, calls
        with mutex:
            active += 1
            peak = max(peak, active)
            calls += 1
        try:
            release.wait(3)
            return "done"
        finally:
            with mutex:
                active -= 1

    tasks = [asyncio.create_task(run_control_query(scan)) for _ in range(_QUERY_CONCURRENCY + 3)]
    try:
        await _until(lambda: active == _QUERY_CONCURRENCY)
        for task in tasks[:_QUERY_CONCURRENCY + 1]:
            task.cancel()
        await asyncio.sleep(0.03)
        assert calls == _QUERY_CONCURRENCY, "Cancellation released capacity before disk work stopped"
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await _until(lambda: active == 0)
    assert peak == _QUERY_CONCURRENCY
    assert calls == _QUERY_CONCURRENCY + 2
    assert results[-2:] == ["done", "done"]


@pytest.mark.asyncio
async def test_background_query_errors_preserve_repository_response(monkeypatch):
    def fail(_params):
        raise OSError("read unavailable")

    monkeypatch.setattr(project_repository, "load_project_list", fail)
    response = await project_repository.handle_project_request(_request(ReqMethod.PROJECT_LIST))
    assert not response.ok
    assert response.payload["code"] == "INTERNAL_ERROR"
    assert response.payload["error"] == "read unavailable"


@pytest.mark.asyncio
async def test_cancelled_background_failure_is_observed_and_capacity_recovers():
    from jiuwenswarm.server.control.query_executor import run_control_query

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def fail():
        started.set()
        release.wait(2)
        finished.set()
        raise OSError("cancelled reader failed")

    task = asyncio.create_task(run_control_query(fail))
    try:
        await _until(started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await _until(finished.is_set)
        assert await run_control_query(lambda: "next") == "next"
        await asyncio.sleep(0.02)
        assert not errors
    finally:
        release.set()
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_cancel_after_admission_wakeup_preserves_all_query_slots():
    from jiuwenswarm.server.control.query_executor import _QUERY_CONCURRENCY, _query_slots, run_control_query

    slots = _query_slots()
    called = []
    for _ in range(_QUERY_CONCURRENCY):
        for _ in range(_QUERY_CONCURRENCY):
            await asyncio.wait_for(slots.acquire(), 1)
        waiter = asyncio.create_task(run_control_query(lambda: called.append(True)))
        await asyncio.sleep(0)
        # Hand off a permit, then cancel before the waiter can resume.
        slots.release()
        waiter.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert await asyncio.wait_for(run_control_query(lambda: "available"), 1) == "available"
        finally:
            for _ in range(_QUERY_CONCURRENCY - 1):
                slots.release()
    assert not called
    for _ in range(_QUERY_CONCURRENCY):
        await asyncio.wait_for(slots.acquire(), 1)
    for _ in range(_QUERY_CONCURRENCY):
        slots.release()


@pytest.mark.asyncio
async def test_cancellation_after_waiter_error_delivery_observes_exception(monkeypatch):
    from jiuwenswarm.server.control.query_executor import run_control_query

    loop = asyncio.get_running_loop()
    errors = []
    delivered = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    schedule = loop.call_soon
    request = None

    def cancel_before_resume(callback, *args, context=None):
        if (request is not None and getattr(callback, "__self__", None) is request
                and args and isinstance(args[0], asyncio.Future)
                and isinstance(getattr(args[0], "_exception", None), OSError)):
            delivered.append(args[0])
            request.cancel()
        return schedule(callback, *args, context=context)

    def fail():
        raise OSError("delivered reader failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_soon", cancel_before_resume)
            request = asyncio.create_task(run_control_query(fail))
            with pytest.raises(asyncio.CancelledError):
                await request
        assert len(delivered) == 1 and not delivered[0]._log_traceback
        delivered.clear()
        gc.collect()
        await asyncio.sleep(0.02)
        assert not errors
        assert await run_control_query(lambda: "next") == "next"
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_background_directory_lookup_does_not_assemble_enterprise_plane(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils
    from jiuwenswarm.enterprise import plane
    from jiuwenswarm.enterprise.tenant import tenant_context
    from jiuwenswarm.server.control.query_executor import run_control_query

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    monkeypatch.setattr(tenant_context, "_TENANT_AGENT_ROOT_CV", ContextVar("query-root", default=None))
    monkeypatch.setattr(plane, "_PLANE", None)
    monkeypatch.setattr(plane, "_BUILD_ATTEMPTED", False)

    def unexpected_assembly(*_args, **_kwargs):
        pytest.fail("A directory lookup must not assemble the enterprise plane")

    monkeypatch.setattr(plane, "try_build_enterprise_plane", unexpected_assembly)
    paths = await asyncio.gather(*(run_control_query(utils.get_agent_sessions_dir) for _ in range(4)))
    assert all(path.is_relative_to(tmp_path) and path.name == "sessions" for path in paths)
    assert plane._PLANE is None and not plane._BUILD_ATTEMPTED
