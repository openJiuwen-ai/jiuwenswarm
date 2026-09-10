# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_search


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def queue(monkeypatch):
    events, responses, executed, cancels = [], [], [], []
    gates = {name: asyncio.Event() for name in "ABC"}
    cancel_ack = asyncio.Event()
    cancel_ack.set()
    fail_cancel = [False]

    async def execute(_client, **kwargs):
        name = kwargs["question"]
        executed.append(name)
        await gates[name].wait()
        return {"answer": name, "realtime_brief": {"summary": name}}

    async def send_event(ws, name, payload):
        events.append((name, dict(payload)))

    async def send_response(ws, req_id, **kwargs):
        responses.append(kwargs)

    async def send_request(env):
        cancels.append(env)
        await cancel_ack.wait()
        return SimpleNamespace(
            ok=not fail_cancel[0],
            payload={"error": "stop rejected"} if fail_cancel[0] else {},
        )

    monkeypatch.setattr(video_search, "execute_core_agent", execute)
    manager = video_search.VideoSearchManager(
        SimpleNamespace(send_event=send_event, send_response=send_response),
        SimpleNamespace(send_request=send_request),
        log_event=lambda _event: None,
        qwen_active=lambda: True,
    )

    def start(name, scope="scope"):
        return manager.start(None, question=name, query=name, search_session_id=scope)[
            "id"
        ]

    async def control(job_id, action, **extra):
        await manager.handle_control(
            None,
            "request",
            {
                "search_session_id": "scope",
                "job_id": job_id,
                "action": action,
                "queue_version": manager._queue_versions.get("scope", 0),
                **extra,
            },
            None,
        )
        return responses[-1]

    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_reorder_changes_execution_and_cancelled_waiter_never_runs(queue):
    q = queue
    a = q.start("A")
    await until(lambda: q.executed == ["A"])
    b, c = q.start("B"), q.start("C")
    await until(lambda: "progress_history" in q.manager._jobs[c])
    assert (await q.control(c, "next"))["ok"]
    assert q.manager._queue["scope"] == [c, b]
    assert (await q.control(b, "cancel"))["ok"]
    q.gates["A"].set()
    q.gates["C"].set()
    await asyncio.gather(*q.manager._tasks)
    assert q.executed == ["A", "C"]
    assert q.manager._jobs[b]["status"] == "cancelled"
    assert q.manager._jobs[a]["status"] == "completed"
    assert not q.cancels


@pytest.mark.asyncio
async def test_preempt_waits_for_real_cancel_ack_then_runs_selected_job(queue):
    q = queue
    a = q.start("A")
    await until(lambda: q.executed == ["A"])
    q.start("B")
    c = q.start("C")
    await until(lambda: "progress_history" in q.manager._jobs[c])
    q.cancel_ack.clear()
    operation = asyncio.create_task(q.control(c, "preempt"))
    await until(lambda: len(q.cancels) == 1)
    assert q.manager._jobs[a]["status"] == "cancelling"
    assert q.executed == ["A"]
    assert (
        q.cancels[0].session_id == q.manager._session_state("scope")["core_session_id"]
    )
    q.gates["B"].set()
    q.gates["C"].set()
    q.cancel_ack.set()
    assert (await operation)["ok"]
    await asyncio.gather(*q.manager._tasks)
    assert q.executed == ["A", "C", "B"]
    assert q.manager._jobs[a]["status"] == "cancelled"
    assert not any(
        name == "video.search.completed" and data["job_id"] == a
        for name, data in q.events
    )


@pytest.mark.asyncio
async def test_rejected_cancel_does_not_claim_success_or_start_next(queue):
    q = queue
    a = q.start("A")
    await until(lambda: q.executed == ["A"])
    q.start("B")
    q.fail_cancel[0] = True
    assert not (await q.control(a, "cancel"))["ok"]
    assert q.manager._jobs[a]["status"] == "running"
    assert q.executed == ["A"]
    q.gates["A"].set()
    q.gates["B"].set()
    await asyncio.gather(*q.manager._tasks)
    assert q.executed == ["A", "B"]


@pytest.mark.asyncio
async def test_stale_order_and_wrong_owner_are_rejected(queue):
    q = queue
    q.start("A")
    await until(lambda: q.executed == ["A"])
    b = q.start("B")
    assert not (await q.control(b, "next", queue_version=-1))["ok"]
    assert not (await q.control(b, "cancel", search_session_id="other"))["ok"]
    assert q.manager._jobs[b]["status"] == "queued"
    assert (await q.control(b, "cancel"))["ok"]
    q.gates["A"].set()
    await asyncio.gather(*q.manager._tasks)
    assert q.executed == ["A"]


@pytest.mark.asyncio
async def test_cancel_completed_job_never_interrupts_next_task(queue):
    q = queue
    a = q.start("A")
    q.start("B")
    q.gates["A"].set()
    await until(lambda: q.executed == ["A", "B"])
    assert (await q.control(a, "cancel"))["ok"]
    assert not q.cancels
    q.gates["B"].set()
    await asyncio.gather(*q.manager._tasks)


@pytest.mark.asyncio
async def test_cancel_before_runner_starts_does_not_dispatch_core_request(queue):
    q = queue
    a = q.start("A")
    assert (await q.control(a, "cancel"))["ok"]
    await asyncio.gather(*q.manager._tasks)
    assert not q.executed
    assert not q.cancels
    assert q.manager._jobs[a]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_failed_preemption_restores_pending_order(queue):
    q = queue
    q.start("A")
    await until(lambda: q.executed == ["A"])
    b, c = q.start("B"), q.start("C")
    await until(lambda: "progress_history" in q.manager._jobs[c])
    q.fail_cancel[0] = True
    assert not (await q.control(c, "preempt"))["ok"]
    assert q.manager._queue["scope"] == [b, c]
    for gate in q.gates.values():
        gate.set()
    await asyncio.gather(*q.manager._tasks)
    assert q.executed == ["A", "B", "C"]
