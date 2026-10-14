"""JM-12: stop and cancel controls on one Request."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3

import pytest
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from test_session_scheduler import ACCESS, NOW, NS, Host, settle

from openjiuwen.harness import persistent_agent as core


@pytest.fixture
async def host(tmp_path):
    host = Host(tmp_path)
    await host.manager.start()
    yield host
    host.core.release(*host.core.started)
    await host.manager.aclose()


async def control(host, state, reason="stop", access=ACCESS):
    return await host.manager.control_request(NS, state.record.request_id, reason=reason, access=access)


def stored_controls(host) -> list[tuple]:
    with sqlite3.connect(host.manager.db_path) as conn:
        return conn.execute("SELECT request_id, reason, status FROM request_controls").fetchall()


# JM-12-V-01 only the sender controls a Request; the control reaches only the target


async def test_stopping_a_running_request_ends_it_as_stopped_and_records_the_control(host):
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])

    view = await control(host, state, "stop")
    await host.scheduler.drain()

    assert view.control.reason == "stop"
    assert view.control.status == "accepted"
    assert host.status(state) == ("ended", "stopped")
    assert host.core.aborts == [(state.record.request_id, "stop")]
    ended = await host.manager.request_view(NS, state.record.request_id)
    assert ended.to_dict()["control"]["reason"] == "stop"


async def test_a_control_from_another_principal_is_refused_and_leaves_no_trace(host):
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])
    stranger = dataclasses.replace(ACCESS, principal_ref="user-2")

    with pytest.raises(PersistentAgentError) as exc_info:
        await control(host, state, "stop", access=stranger)

    assert exc_info.value.code == "FORBIDDEN"
    assert host.core.aborts == []
    assert stored_controls(host) == []
    assert host.status(state) == ("dispatched", None)


async def test_a_control_reaches_only_its_target_session(host):
    target = await host.send("目标", conversation="conv-1")
    other = await host.send("旁路", conversation="conv-2")
    await settle(lambda: len(host.core.started) == 2)

    await control(host, target, "cancel")
    await settle(lambda: host.status(target)[0] == "ended")

    assert host.status(target) == ("ended", "cancelled")
    assert host.status(other) == ("dispatched", None)
    assert host.core.aborts == [(target.record.request_id, "cancel")]


# JM-12-V-02 a repeated control returns the stored fact without interrupting again


async def test_a_repeated_control_returns_the_first_fact(host):
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])

    first = await control(host, state, "stop")
    await host.scheduler.drain()
    again = await control(host, state, "cancel")

    assert again.control == first.control
    assert again.control.reason == "stop"
    assert host.core.aborts == [(state.record.request_id, "stop")]
    assert stored_controls(host) == [(state.record.request_id, "stop", "accepted")]


async def test_concurrent_controls_interrupt_once(host):
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])

    views = await asyncio.gather(*(control(host, state, reason) for reason in ("stop", "cancel", "stop")))
    await host.scheduler.drain()

    assert len({view.control.reason for view in views}) == 1
    assert len(host.core.aborts) == 1


async def test_a_request_that_ended_without_control_cannot_be_controlled(host):
    state = await host.send("任务")
    host.core.release("任务")
    await host.scheduler.drain()

    with pytest.raises(PersistentAgentError) as exc_info:
        await control(host, state, "stop")

    assert exc_info.value.code == "CONFLICT"
    assert stored_controls(host) == []


async def test_an_unknown_request_or_reason_is_refused(host):
    state = await host.send("任务")

    with pytest.raises(PersistentAgentError) as unknown:
        await host.manager.control_request(NS, "req-missing", reason="stop", access=ACCESS)
    with pytest.raises(PersistentAgentError) as bad_reason:
        await control(host, state, "pause")

    assert unknown.value.code == "NOT_FOUND"
    assert bad_reason.value.code == "BAD_REQUEST"


# JM-12-V-03 queued and restart-left Requests end directly; the Session moves on


async def test_cancelling_a_queued_request_ends_it_without_running(host):
    await host.send("一")
    queued = await host.send("二")
    await settle(lambda: host.core.started == ["一"])

    view = await control(host, queued, "cancel")
    host.core.release("一")
    await host.scheduler.drain()

    assert view.state.outcome == "cancelled"
    assert host.status(queued) == ("ended", "cancelled")
    assert host.core.started == ["一"]
    assert host.core.aborts == []


async def test_a_request_left_dispatched_by_a_restart_ends_and_releases_its_session(tmp_path):
    before = Host(tmp_path)
    session = before.session()
    for text in ("中断", "排队"):
        before.sessions.accept_request(
            session,
            snapshot=before.service.snapshot(NS, "mate_dev", "v1"),
            source_ref=f"source:{text}",
            access=ACCESS,
            request_input=core.RequestInput(kind="scheduled", text=text, reply_target="conv-1"),
            received_at=NOW,
        )
    interrupted, queued = before.sessions.session_requests(NS, session.session_id)
    before.sessions.start_execution(NS, interrupted.record.request_id, now=NOW)

    host = Host(tmp_path)
    await host.manager.start()
    try:
        await host.scheduler.recover(NS)
        assert host.core.started == []

        view = await control(host, interrupted, "stop")
        await settle(lambda: host.core.started == ["排队"])
        host.core.release("排队")
        await host.scheduler.drain()

        assert view.state.outcome == "stopped"
        assert host.core.aborts == []
        assert host.status(queued) == ("ended", "completed")
    finally:
        await host.manager.aclose()


# JM-12-V-04 a failed interruption keeps the control and marks it failed


async def test_a_failed_interruption_keeps_the_control_as_failed(host):
    state = await host.send("任务")
    await settle(lambda: host.core.started == ["任务"])

    async def broken(context, *, reason):
        raise RuntimeError("abort crashed")

    handle = host.scheduler._run_of(NS, state.record.request_id).handle
    handle._abort = broken
    view = await control(host, state, "stop")
    handle._abort = host.core.abort

    assert view.control.status == "failed"
    assert view.control.detail == "abort crashed"
    assert stored_controls(host) == [(state.record.request_id, "stop", "failed")]
    again = await control(host, state, "stop")
    assert again.control.status == "failed"
