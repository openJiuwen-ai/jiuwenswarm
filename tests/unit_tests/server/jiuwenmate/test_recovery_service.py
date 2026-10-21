"""JM-13: Requests a previous process left dispatched are judged from their log and never re-run."""

from __future__ import annotations

import asyncio

import pytest
from jiuwenswarm.server.jiuwenmate.execution_control_service import ControlStore
from jiuwenswarm.server.jiuwenmate.execution_models import RequestRouteSnapshot, build_execution_context
from jiuwenswarm.server.jiuwenmate.recovery_service import RecoveryStore, RequestRecovery
from test_session_scheduler import ACCESS, NOW, NS, Host

from openjiuwen.harness import persistent_agent as core

DIGEST = "0" * 64


def intent(call_id: str) -> tuple:
    return core.EventType.TOOL_INTENT, {
        "operation_id": f"op-{call_id}",
        "tool_call_id": call_id,
        "tool_name": "write_file",
        "params_digest": DIGEST,
        "args_summary": "写入报告",
    }


def completed(call_id: str) -> tuple:
    return core.EventType.TOOL_COMPLETED, {
        "tool_call_id": call_id,
        "tool_name": "write_file",
        "args_summary": "写入报告",
        "status": "succeeded",
        "result_summary": "已写入",
        "operation_id": f"op-{call_id}",
    }


FINAL = (core.EventType.EXECUTION_FINAL, {"status": "completed", "output_refs": ()})
STOPPED = (core.EventType.EXECUTION_PAUSED, {"reason": "stopped"})


class Left:
    """Data a previous process left: the first Request of a Session dispatched with the given log, the next queued."""

    def __init__(self, tmp_path, events=(), *, control: str | None = None) -> None:
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
        self.interrupted, self.queued = before.sessions.session_requests(NS, session.session_id)
        execution = before.sessions.start_execution(NS, self.interrupted.record.request_id, now=NOW)
        self.execution_id = execution.execution_id
        context = build_execution_context(
            self.interrupted.record, execution, capability_snapshot=frozenset({"construction"})
        )
        saved = [
            core.AgentEvent.create(context, event_type=event_type, local_seq=seq, occurred_at=NOW, payload=payload)
            for seq, (event_type, payload) in enumerate(events, start=1)
        ]
        if saved:
            before.sessions.record_events(saved, now=NOW)
        if control is not None:
            ControlStore(before.sessions.db_path).claim(
                NS, self.interrupted.record.request_id, reason=control, principal_ref="user-1", now=NOW
            )


@pytest.fixture
async def restart(tmp_path):
    hosts = []

    async def start(left: Left) -> tuple[Host, list[RequestRecovery]]:
        host = Host(tmp_path)
        await host.manager.start()
        hosts.append(host)
        recovered = await host.manager.recover(NS)
        host.core.release("中断", "排队")
        await host.scheduler.drain()
        return host, recovered

    yield start
    for host in hosts:
        await host.manager.aclose()


# JM-13-V-01 the latest Execution's final / paused decides; without it or a control, the Request awaits continuation


async def test_a_saved_final_closes_the_request_without_running_it_again(tmp_path, restart):
    left = Left(tmp_path, [FINAL])

    host, recovered = await restart(left)

    assert [(r.decision, r.outcome) for r in recovered] == [("closed_from_log", "completed")]
    assert host.status(left.interrupted) == ("ended", "completed")
    assert host.core.started == ["排队"]
    assert host.status(left.queued) == ("ended", "completed")


async def test_a_saved_stop_closes_the_request_as_stopped(tmp_path, restart):
    left = Left(tmp_path, [STOPPED])

    host, recovered = await restart(left)

    assert [(r.decision, r.outcome) for r in recovered] == [("closed_from_log", "stopped")]
    assert host.status(left.interrupted) == ("ended", "stopped")


async def test_an_interrupted_request_is_paused_awaiting_continuation_and_its_session_moves_on(tmp_path, restart):
    left = Left(tmp_path, [(core.EventType.EXECUTION_STARTED, {"goal": "中断"})])

    host, recovered = await restart(left)

    assert [(r.decision, r.outcome, r.execution_id) for r in recovered] == [
        ("awaiting_continuation", "paused", left.execution_id)
    ]
    assert host.core.started == ["中断", "排队"]
    continued = host.core.contexts["中断"].execution
    assert continued.recovery_ref == left.execution_id
    assert continued.execution_id != left.execution_id
    assert host.status(left.interrupted) == ("ended", "completed")
    assert host.status(left.queued) == ("ended", "completed")
    link = host.sessions.execution_link(NS, left.interrupted.record.request_id, continued.execution_id)
    assert link["relation"] == "continuation" and link["predecessor_id"] == left.execution_id
    view = (await host.manager.request_view(NS, left.interrupted.record.request_id)).to_dict()
    assert view["recovery"]["decision"] == "awaiting_continuation"
    assert view["recovery"]["execution_id"] == left.execution_id


# JM-13-V-02 a stopped or cancelled Request is not continued; a judgment is made once per Execution


async def test_a_control_of_the_sender_closes_an_interrupted_request_without_continuation(tmp_path, restart):
    left = Left(tmp_path, [(core.EventType.EXECUTION_STARTED, {"goal": "中断"})], control="cancel")

    host, recovered = await restart(left)

    assert [(r.decision, r.outcome) for r in recovered] == [("closed_by_control", "cancelled")]
    assert host.status(left.interrupted) == ("ended", "cancelled")


async def test_a_saved_terminal_event_wins_over_a_control(tmp_path, restart):
    left = Left(tmp_path, [FINAL], control="stop")

    _, recovered = await restart(left)

    assert [(r.decision, r.outcome) for r in recovered] == [("closed_from_log", "completed")]


async def test_a_second_restart_neither_judges_nor_closes_again(tmp_path, restart):
    left = Left(tmp_path, [(core.EventType.EXECUTION_STARTED, {"goal": "中断"})])
    first, _ = await restart(left)
    await first.manager.aclose()

    second, recovered = await restart(left)

    assert recovered == []
    assert second.status(left.interrupted) == ("ended", "completed")
    assert second.core.started == []


def test_the_first_judgment_of_an_execution_is_kept(tmp_path):
    store = RecoveryStore(tmp_path / "agents.sqlite3")
    first = RequestRecovery(
        request_id="req-1",
        execution_id="exe-1",
        decision="awaiting_continuation",
        outcome="paused",
        unverified_tool_calls=("call-1",),
        decided_at=NOW,
    )
    other = RequestRecovery(
        request_id="req-1",
        execution_id="exe-1",
        decision="closed_by_control",
        outcome="stopped",
        unverified_tool_calls=(),
        decided_at=NOW,
    )

    assert store.claim(NS, first) == first
    assert store.claim(NS, other) == first
    assert store.latest(NS, "req-1") == first


# JM-13-V-03 a tool intent without a saved completion is unverified, not "not executed"


async def test_tool_calls_without_a_saved_completion_are_recorded_as_unverified(tmp_path, restart):
    left = Left(
        tmp_path,
        [
            (core.EventType.EXECUTION_STARTED, {"goal": "中断"}),
            intent("call-done"),
            completed("call-done"),
            intent("call-open"),
        ],
    )

    host, recovered = await restart(left)

    assert recovered[0].unverified_tool_calls == ("call-open",)
    assert host.core.started == ["中断", "排队"]
    view = (await host.manager.request_view(NS, left.interrupted.record.request_id)).to_dict()
    assert view["recovery"]["unverified_tool_calls"] == ["call-open"]


async def test_web_recovery_leaves_a_dispatched_feishu_request_for_the_feishu_runtime(tmp_path):
    before = Host(tmp_path)
    feishu_session = before.session(conversation="feishu")
    web_session = before.session(conversation="web")
    snapshot = before.service.snapshot(NS, "mate_dev", "v1")
    route = RequestRouteSnapshot(
        channel="feishu", app_id="cli_app", target_id="oc_group", source_message_id="om_1",
    )
    for session, text, route_snapshot in (
        (feishu_session, "飞书", route),
        (web_session, "网页", None),
    ):
        before.sessions.accept_request(
            session,
            snapshot=snapshot,
            source_ref=f"source:{text}",
            access=ACCESS,
            request_input=core.RequestInput(kind="scheduled", text=text, reply_target=session.conversation.key),
            received_at=NOW,
            route_snapshot=route_snapshot,
        )
    feishu = before.sessions.session_requests(NS, feishu_session.session_id)[0]
    web = before.sessions.session_requests(NS, web_session.session_id)[0]
    before.sessions.start_execution(NS, feishu.record.request_id, now=NOW)
    before.sessions.start_execution(NS, web.record.request_id, now=NOW)

    host = Host(tmp_path)
    await host.manager.start()
    recovered = await host.manager.recover(NS, channels=frozenset({"web"}), include_unrouted=True)
    assert [item.request_id for item in recovered] == [web.record.request_id]
    assert host.sessions.get_request(NS, feishu.record.request_id).status == "dispatched"
    host.core.release("网页")
    await host.manager.aclose()

    resumed = Host(tmp_path)
    await resumed.manager.start()
    recovered = await resumed.manager.recover(NS, channels=frozenset({"feishu"}), include_unrouted=False)
    assert [item.request_id for item in recovered] == [feishu.record.request_id]
    for _ in range(50):
        if "飞书" in resumed.core.started:
            break
        await asyncio.sleep(0.02)
    assert "飞书" in resumed.core.started
    resumed.core.release("飞书")
    await resumed.scheduler.drain()
    await resumed.manager.aclose()
