"""Execution facts survive checkpoint and supplemental transport failures."""

# These regressions deliberately control the scheduler, writer and notification
# internals to hold the exact failure window open.
# pylint: disable=protected-access

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.runner.callback.errors import AbortError

from jiuwenswarm.extensions.video_duplex.backend import task_adapter
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import bind_task_execution, bind_task_output
from jiuwenswarm.extensions.video_duplex.backend.tasks.rail import task_checkpoint
from jiuwenswarm.extensions.video_duplex.backend.tasks.server_adapter import VoiceTaskServerAdapter
from jiuwenswarm.extensions.video_duplex.tests.backend import test_task_bridge

routed_task_fixture = test_task_bridge.routed_task_fixture


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("failure", ["cancel", "query"])
@pytest.mark.parametrize("scheduled", [True, False])
async def test_first_checkpoint_failure_waits_for_native_cleanup(routed_task, failure, scheduled):
    r = routed_task
    root = object()
    rail = SimpleNamespace(managed_tasks={})
    scheduler = SimpleNamespace(_running_tasks={})
    harness = SimpleNamespace(
        _react_agent=root, active_round=SimpleNamespace(task_id="native"),
        loop_controller=SimpleNamespace(task_scheduler=scheduler),
    )
    adapter = SimpleNamespace(
        _is_session_scoped_adapter=True, task_execution_binding=(harness, rail),
    )
    ctx = SimpleNamespace(
        agent=root, extra={"run_context": {"extra": {"managed_task_request": r.task["request_id"]}}},
    )
    cleanup_started, release = asyncio.Event(), asyncio.Event()

    async def native_run():
        try:
            await task_checkpoint(rail, ctx, "before_model")
        except AbortError:
            cleanup_started.set()
            await release.wait()

    native = None
    try:
        async with bind_task_execution(r.request, adapter, {}):
            if failure == "cancel":
                r.store.update(r.task["id"], lambda t: t.update(status="cancelling"))
            else:
                rail.managed_tasks[r.request.session_id].check = AsyncMock(side_effect=TimeoutError)
            native = asyncio.create_task(native_run())
            if scheduled:
                scheduler._running_tasks["native"] = (None, native)
            await asyncio.wait_for(cleanup_started.wait(), 2)
        assert not native.done()
        assert not r.store.read(r.task["id"])["execution_settled"]
        release.set()
        await native
        await until(lambda: r.store.read(r.task["id"])["execution_settled"])
    finally:
        release.set()
        if native:
            await native


@pytest.mark.parametrize("failure", ["rejected", "timeout", "none"])
async def test_file_lookup_does_not_erase_execution_result(tmp_path, monkeypatch, failure):
    store = TaskStore(tmp_path / "tasks.sqlite")
    existing = {"name": "plan.md", "path": "/out/plan.md"}
    extra = {"name": "budget.md", "path": "/out/budget.md"}
    query = AsyncMock(return_value=SimpleNamespace(ok=failure != "rejected", payload={
        "error": "File history unavailable", "files": [extra],
    }))
    if failure == "timeout":
        query.side_effect = TimeoutError
    executor = task_adapter.AgentTaskExecutor(SimpleNamespace(send_request=query), store, None)

    async def completed(task, progress, context):
        store.update(task["id"], lambda t: t.update(execution_bound=True, execution_settled=True))
        return {"answer": "Meeting plan finished", "files": [existing]}

    monkeypatch.setattr(executor, "_run", completed)
    service = TaskService(store, executor)
    try:
        task = service.submit("alice", "voice", "create", "Plan a meeting")
        await until(lambda: store.read(task["id"])["output_closed"])
        record = store.read(task["id"])
        assert record["status"] == "completed"
        assert record["result"]["answer"] == "Meeting plan finished"
        assert existing in record["result"]["files"]
        public = task_adapter.VideoSearchManager.public(record)
        assert public["artifact_lookup_failed"] is (failure != "none")
        if failure == "none":
            assert extra in record["result"]["files"]
    finally:
        await service.close()


@pytest.mark.parametrize("earlier_lookup_fails", [False, True])
async def test_files_survive_multiple_clarification_rounds(tmp_path, monkeypatch, earlier_lookup_fails):
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata

    records, requests, queries = [], [], []
    draft = {"name": "draft.md", "path": "/out/draft.md"}
    budget = {"name": "budget.md", "path": "/out/budget.md"}
    final_file = {"name": "final.md", "path": "/out/final.md"}
    monkeypatch.setattr(lifecycle, "guard", lambda *_: None)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: {"user_id": "alice"})
    monkeypatch.setattr(session_history, "flush_pending_writes", lambda **k: True)
    monkeypatch.setattr(session_history, "load_history_records", lambda *a, **k: records)
    store = TaskStore(tmp_path / "tasks.sqlite")

    class Client:
        async def send_request(self, env):
            queries.append(env.params["execution_request_id"])
            if earlier_lookup_fails and queries[-1] == requests[0].request_id:
                raise ConnectionError("Earlier attachment query unavailable")
            return await VoiceTaskServerAdapter().handle(e2a_to_agent_request(env))

        async def send_request_stream(self, env):
            requests.append(env)
            identity = {
                **env.params["managed_task_binding"], "owner": "alice",
                "session_id": env.session_id, "request_id": env.request_id,
            }

            def checkpoint(action):
                executor.endpoint.execute({
                    **identity, "action": action, "command_id": env.request_id + ":" + action,
                })

            checkpoint("bind")
            # Native file tools persist/push chat.file separately from this response stream.
            files = [draft] if len(requests) == 1 else [budget] if len(requests) == 2 else [budget, final_file]
            records.append({"request_id": env.request_id, "event_type": "chat.file", "files": files})
            records.append({"request_id": "unrelated", "event_type": "chat.file", "files": [
                {"name": "unrelated.md", "path": "/out/unrelated.md"},
            ]})
            if len(requests) < 3:
                yield SimpleNamespace(payload={
                    "event_type": "chat.ask_user_question", "source": "ask_user_interrupt",
                    "request_id": "question-" + str(len(requests)), "questions": [{"question": "Confirm?"}],
                })
            else:
                yield SimpleNamespace(payload={"event_type": "chat.final", "content": "Plan completed"})
            checkpoint("close")
            checkpoint("settle")

    executor = task_adapter.AgentTaskExecutor(Client(), store, None)
    service = TaskService(store, executor)
    try:
        task = service.submit("alice", "voice", "create", "Create a plan and confirm each stage")
        for index in range(2):
            await until(lambda: store.read(task["id"])["output_closed"])
            waiting = store.read(task["id"])
            assert waiting["status"] == "waiting_user"
            await service.answer(
                "alice", "voice", task["id"], "answer-" + str(index), waiting["interaction"]["id"], answers=["Yes"],
            )
            await until(lambda: len(requests) > index + 1)
        await until(lambda: store.read(task["id"])["status"] == "completed")
        final = TaskStore(store.path).read(task["id"])
        assert len({request.session_id for request in requests}) == 1
        assert len({request.request_id for request in requests}) == 3
        assert final["result"]["answer"] == "Plan completed"
        expected = [budget, final_file] if earlier_lookup_fails else [draft, budget, final_file]
        assert [{key: item[key] for key in ("name", "path")} for item in final["result"].get("files", [])] == expected
        public = task_adapter.VideoSearchManager.public(final)
        assert public["files"] == final["result"]["files"]
        assert public["artifact_lookup_failed"] is earlier_lookup_fails
        assert set(queries) == {request.request_id for request in requests}
    finally:
        await service.close()


def subscribed_manager(tmp_path, metadata):
    channel = SimpleNamespace(send_event=AsyncMock())
    manager = task_adapter.VideoSearchManager(
        channel, SimpleNamespace(send_request=metadata), path=tmp_path / "tasks.sqlite",
        log_event=lambda _: None, qwen_active=lambda: True, authorize=lambda ws, scope: ("alice", scope),
    )
    scope = "task-duplex:saved"
    task = TaskService._new("alice", scope, "Plan a meeting", {})
    task.update(status="completed", result={"answer": "Done"})
    # Persist the completed record before startup/recovery and queue projections.
    with manager.service.store.transaction() as db:
        manager.service.store.put(db, task)
    ws = object()
    manager.subscribers[("alice", scope, id(ws))] = ws
    return manager, task


async def test_valid_slow_metadata_still_delivers_completion(tmp_path):
    async def metadata(env):
        await asyncio.sleep(2.1)
        return SimpleNamespace(ok=True, payload={"user_id": "alice"})

    manager, task = subscribed_manager(tmp_path, metadata)
    try:
        await manager.service._notify(task)
        await until(lambda: not manager.service.notifications)
        assert manager.channel.send_event.await_count == 2
        assert manager.channel.send_event.call_args_list[0].args[1] == "video.search.completed"
    finally:
        await manager.close()


@pytest.mark.parametrize("transient", [True, False])
async def test_notification_retries_transport_but_rejects_wrong_owner(tmp_path, transient):
    query = AsyncMock(side_effect=[
        ConnectionError("temporarily offline") if transient else
        SimpleNamespace(ok=True, payload={"user_id": "mallory"}),
        SimpleNamespace(ok=True, payload={"user_id": "alice"}),
    ])
    manager, task = subscribed_manager(tmp_path, query)
    try:
        await manager.service._notify(task)
        await until(lambda: not manager.service.notifications)
        assert manager.channel.send_event.await_count == (2 if transient else 0)
        assert query.await_count == (2 if transient else 1)
        assert bool(manager.subscribers) is transient
    finally:
        await manager.close()


async def test_retry_delivers_newest_snapshot_and_stays_bounded(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def notify(task):
        delivered.append(task["status"])
        if len(delivered) == 1:
            entered.set()
            await release.wait()
        raise ConnectionError("offline")

    service = TaskService(TaskStore(tmp_path / "tasks.sqlite"), None, on_change=notify)
    try:
        await service._notify({"id": "task", "status": "running"})
        await entered.wait()
        await service._notify({"id": "task", "status": "completed"})
        release.set()
        await until(lambda: not service.notifications)
        assert delivered == ["running", "completed", "completed", "completed"]
        assert not service.pending_notifications
    finally:
        release.set()
        await service.close()


async def test_file_query_waits_for_enqueued_history(routed_task, monkeypatch):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    r = routed_task
    sid = r.task["core_session_id"]
    entered, release, barrier_entered = threading.Event(), threading.Event(), threading.Event()
    original_write = session_history._write_item
    original_flush = session_history.flush_pending_writes
    item = {"name": "plan.md", "path": "/out/plan.md"}

    def delayed_write(session, record, **kwargs):
        if session == sid:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test writer was not released")
        return original_write(session, record, **kwargs)

    def observe_barrier(**kwargs):
        barrier_entered.set()
        return original_flush(**kwargs)

    monkeypatch.setattr(session_history, "_write_item", delayed_write)
    monkeypatch.setattr(session_history, "flush_pending_writes", observe_barrier)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: {"user_id": "alice"})
    request = SimpleNamespace(
        req_method=ReqMethod.VOICE_TASK_FILES, session_id=sid, user_id="alice",
        request_id="query", channel_id="video_tool", metadata=None,
        params={"execution_request_id": r.task["request_id"]},
    )
    query = None
    try:
        session_history.append_history_record(
            session_id=sid, request_id=r.task["request_id"], channel_id="video_tool",
            role="assistant", content="", timestamp=1, event_type="chat.file", extra={"files": [item]},
        )
        assert await asyncio.to_thread(entered.wait, 2)
        query = asyncio.create_task(VoiceTaskServerAdapter().handle(request))
        assert await asyncio.to_thread(barrier_entered.wait, 2)
        assert not query.done()
        release.set()
        response = await query
        assert [f["name"] for f in response.payload["files"]] == ["plan.md"]
    finally:
        release.set()
        if query:
            await asyncio.gather(query, return_exceptions=True)
        await asyncio.to_thread(original_flush, timeout=2)


async def test_file_query_never_reports_empty_success_on_history_timeout(monkeypatch):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata

    monkeypatch.setattr(lifecycle, "guard", lambda *_: None)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: {"user_id": "alice"})
    monkeypatch.setattr(session_history, "flush_pending_writes", lambda **kwargs: False)
    request = SimpleNamespace(
        req_method=ReqMethod.VOICE_TASK_FILES, session_id="managed-task-" + "a" * 32,
        user_id="alice", params={"execution_request_id": "run"},
    )
    with pytest.raises(TimeoutError, match="not yet durable"):
        await VoiceTaskServerAdapter().handle(request)


async def test_output_check_failure_before_model_waits_for_native_execution(routed_task):
    r = routed_task
    release = asyncio.Event()
    native = asyncio.create_task(release.wait())
    rail = SimpleNamespace(managed_tasks={})
    harness = SimpleNamespace(
        _react_agent=object(), active_round=SimpleNamespace(task_id="native"),
        loop_controller=SimpleNamespace(task_scheduler=SimpleNamespace(_running_tasks={"native": (None, native)})),
    )
    adapter = SimpleNamespace(_is_session_scoped_adapter=True, task_execution_binding=(harness, rail))
    try:
        async with bind_task_execution(r.request, adapter, {}):
            binding = rail.managed_tasks[r.request.session_id]
            original_call = binding.endpoint.call

            async def fail_status(action, **kwargs):
                if action == "status":
                    raise TimeoutError("status unavailable")
                return await original_call(action, **kwargs)

            binding.endpoint.call = fail_status
            with pytest.raises(TimeoutError):
                await bind_task_output(rail, r.request, SimpleNamespace(close=AsyncMock()))
            # The scheduler can discard its entry before cancellation cleanup ends.
            harness.loop_controller.task_scheduler._running_tasks.clear()
        assert not r.store.read(r.task["id"])["execution_settled"]
        release.set()
        await native
        await until(lambda: r.store.read(r.task["id"])["execution_settled"])
    finally:
        release.set()
        await native


async def test_blocked_subscriber_does_not_delay_another_subscriber(tmp_path):
    metadata = AsyncMock(return_value=SimpleNamespace(ok=True, payload={"user_id": "alice"}))
    manager, task = subscribed_manager(tmp_path, metadata)
    slow = next(iter(manager.subscribers.values()))
    fast = object()
    manager.subscribers[("alice", task["session"], id(fast))] = fast
    blocked, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    slow_finished = asyncio.Event()

    async def send(ws, event, payload):
        if ws is slow:
            blocked.set()
            try:
                await release.wait()
            finally:
                slow_finished.set()
        elif event == "video.search.completed":
            delivered.set()

    manager.channel.send_event = AsyncMock(side_effect=send)
    try:
        await manager.service._notify(task)
        await asyncio.wait_for(blocked.wait(), 2)
        await asyncio.wait_for(delivered.wait(), 2)
        assert not slow_finished.is_set()
        release.set()
        await until(lambda: not manager.service.notifications)
    finally:
        release.set()
        await manager.close()


async def test_partial_notification_failure_retries_without_reexecuting_task(tmp_path):
    metadata = AsyncMock(return_value=SimpleNamespace(ok=True, payload={"user_id": "alice"}))
    manager, task = subscribed_manager(tmp_path, metadata)
    events = []
    failed = False

    async def send(ws, event, payload):
        nonlocal failed
        if event == "video.search.queue" and not failed:
            failed = True
            raise ConnectionError("Queue event was not delivered")
        events.append((event, payload))

    manager.channel.send_event = AsyncMock(side_effect=send)
    manager.service.executor.run = AsyncMock(side_effect=AssertionError("Notification must not execute tasks"))
    before = manager.service.store.read(task["id"])
    try:
        await manager.service._notify(task)
        await until(lambda: not manager.service.notifications)
        completions = [payload for event, payload in events if event == "video.search.completed"]
        assert completions
        # Retries may repeat a completion, but must retain its identity and sequence.
        assert all(payload == completions[0] for payload in completions)
        assert events[-1][0] == "video.search.queue"
        assert manager.service.store.read(task["id"]) == before
        manager.service.executor.run.assert_not_awaited()
    finally:
        await manager.close()
