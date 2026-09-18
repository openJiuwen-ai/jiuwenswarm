"""Video adapter regressions over the persisted task service (no private queue mirrors)."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_search
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import task_identity


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
async def queue(monkeypatch, tmp_path):
    events, responses, executed, cancels = [], [], [], []
    gates = {name: asyncio.Event() for name in "ABC"}
    stop_ack = asyncio.Event()
    stop_ack.set()
    stopped = set()
    reject = [False]

    async def execute(_client, **kwargs):
        name = kwargs["question"]
        executed.append(name)
        await gates[name].wait()
        with manager.service.store.transaction() as db:
            task = next(
                t
                for t in manager.service.store.rows(db)
                if t["request_id"] == kwargs["request_id"]
            )
        manager.service.store.update(
            task["id"], lambda t: t.update(execution_settled=True)
        )
        if name in stopped:
            raise RuntimeError("Stopped")
        return {"answer": name}

    async def send_event(ws, name, payload):
        events.append((name, payload))

    async def send_response(ws, req_id, **kwargs):
        responses.append(kwargs)

    async def send_request(env):
        cancels.append(env)
        await stop_ack.wait()
        if reject[0]:
            return SimpleNamespace(ok=False, payload={})
        with manager.service.store.transaction() as db:
            task = next(
                t
                for t in manager.service.store.rows(db)
                if t["core_session_id"] == env.session_id
            )
        stopped.add(task["instruction"])
        gates[task["instruction"]].set()
        return SimpleNamespace(ok=True, payload={})

    monkeypatch.setattr(video_search, "execute_core_agent", execute)
    manager = video_search.VideoSearchManager(
        SimpleNamespace(send_event=send_event, send_response=send_response),
        SimpleNamespace(send_request=send_request),
        log_event=lambda _: None,
        qwen_active=lambda: True,
        path=tmp_path / "tasks.sqlite",
        authorize=lambda ws, scope: ("alice", scope),
    )

    def start(name, scope="scope"):
        return manager.start(
            None, question=name, query=name, search_session_id=scope, command_id=name
        )["id"]

    def record(task_id):
        return manager.service.get("alice", "scope", task_id)

    async def control(task_id, action, **extra):
        await manager.handle_control(
            None,
            "control-" + action + task_id,
            {
                "search_session_id": "scope",
                "job_id": task_id,
                "action": action,
                "queue_version": manager.snapshot("alice", "scope")["queue_version"],
                **extra,
            },
            None,
        )
        return responses[-1]

    yield SimpleNamespace(**locals())
    await manager.close()


async def test_reorder_and_cancel_waiter(queue):
    q = queue
    q.start("A")
    await until(lambda: q.executed == ["A"])
    b, c = q.start("B"), q.start("C")
    assert (await q.control(c, "next"))["ok"]
    assert (await q.control(b, "cancel"))["ok"]
    q.gates["A"].set()
    q.gates["C"].set()
    await until(lambda: q.record(c)["status"] == "completed")
    assert q.executed == ["A", "C"] and not q.cancels
    assert q.record(b)["status"] == "cancelled"


async def test_preempt_stays_pending_until_exact_stop_ack(queue):
    q = queue
    a = q.start("A")
    await until(lambda: q.executed == ["A"])
    q.start("B")
    c = q.start("C")
    q.stop_ack.clear()
    assert (await q.control(c, "preempt"))["ok"]
    await until(lambda: q.cancels)
    assert q.cancels[0].session_id == q.record(a)["core_session_id"]
    assert q.record(a)["status"] == "cancelling" and q.executed == ["A"]
    q.stop_ack.set()
    q.gates["C"].set()
    await until(lambda: len(q.executed) == 3)
    assert q.executed == ["A", "C", "B"]
    assert q.record(a)["status"] == "cancelled"
    assert not any(
        event == "video.search.completed" and data["job_id"] == a
        for event, data in q.events
    )


async def test_rejected_stop_is_not_falsely_reported_stopped(queue):
    q = queue
    a = q.start("A")
    await until(lambda: q.executed)
    q.start("B")
    q.reject[0] = True
    assert (await q.control(a, "cancel"))["ok"]  # durable admission only
    await until(lambda: q.record(a)["error"])
    assert q.record(a)["status"] == "cancelling" and q.executed == ["A"]


async def test_wrong_scope_and_stale_queue_have_no_effect(queue):
    q = queue
    a = q.start("A")
    await until(lambda: q.executed)
    b = q.start("B")
    assert not (await q.control(b, "next", queue_version=-1))["ok"]
    assert not (await q.control(a, "cancel", search_session_id="other"))["ok"]
    assert q.record(b)["status"] == "queued" and not q.cancels


async def test_query_control_tools_return_receipts_without_creating_work(queue):
    q = queue
    a = q.start("A")
    for call, name, arguments in [
        ("find", "jiuwen_task_query", {"job_id": a}),
        (
            "edit",
            "jiuwen_task_modify",
            {"job_id": a, "revision": 1, "instruction": "French"},
        ),
        ("stop", "jiuwen_task_cancel", {"job_id": a}),
    ]:
        await q.manager.handle_qwen_tool(
            None,
            call,
            {
                "search_session_id": "scope",
                "name": name,
                "call_id": call,
                "arguments": arguments,
            },
            None,
        )
        assert q.responses[-1]["ok"]
        assert "tool_result" in q.responses[-1]["payload"]
    assert len(q.manager.snapshot("alice", "scope")["jobs"]) == 1
    await asyncio.sleep(0.02)
    assert q.executed == q.cancels == []


@pytest.mark.parametrize(
    "remote,origin",
    [
        ("192.0.2.1", "http://127.0.0.1:5173"),
        ("127.0.0.1", "https://untrusted.example"),
        ("127.0.0.1", ""),
    ],
)
def test_untrusted_connections_cannot_claim_local_identity(remote, origin):
    ws = SimpleNamespace(
        remote_address=(remote, 1234),
        request_headers={"Origin": origin},
        _web_connection_user_id="admin",
    )
    with pytest.raises(ValueError):
        task_identity(ws, "scope")


@pytest.mark.parametrize(
    "scope", ["task-duplex:..", "task-duplex:C:other", "task-duplex:alias."]
)
def test_saved_conversation_rejects_filesystem_aliases(scope):
    ws = SimpleNamespace(
        remote_address=("127.0.0.1", 1234),
        request_headers={"Origin": "http://127.0.0.1:5173"},
    )
    with pytest.raises(ValueError, match="Invalid saved conversation"):
        task_identity(ws, scope)
