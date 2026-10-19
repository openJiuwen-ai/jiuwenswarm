# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The sweep: lapsed attachments go, and calls no live attachment shows for the grace are answered as gone."""

from __future__ import annotations

import asyncio
import logging

import pytest

from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
from jiuwenswarm.runtime.session.model import NoControlTargetError
from jiuwenswarm.server.client_attachments import (
    close_client_attachments,
    directory,
    start_client_attachment_sweep,
    watch,
)
from jiuwenswarm.server.client_attachments.directory import LEASE_SECONDS, get_attachment_directory
from jiuwenswarm.server.client_attachments.watch import CLIENT_GONE_GRACE_SECONDS
from tests.unit_tests.agentserver.client_attachments_support import (  # noqa: F401 - the fixture
    RESOURCE,
    PAUSED,
    answer_async,
    answered,
    give_access,
    isolated,
    pending_call,
    pending_question,
    read_paused,
    renew,
    request,
    sweep,
)

pytestmark = pytest.mark.usefixtures("isolated")


@pytest.fixture
def clock():
    """The ``isolated`` fixture's clock."""
    return directory.clock


@pytest.fixture
def gone() -> list[tuple[str, str, str, list[str]]]:
    """The calls AgentServer is asked to answer as gone: ``(session, channel, owner, call ids)``."""
    answers: list = []

    async def client_gone(session_id, channel_id, owner_id, pending) -> None:
        answers.append((session_id, channel_id, owner_id, [p["request_id"] for p in pending]))

    give_access(client_gone=client_gone)
    return answers


@pytest.fixture
def watch_log(caplog, monkeypatch):
    """caplog for the sweep's logger alone: the jiuwenswarm logger does not propagate once it is set up."""
    watch_logger = logging.getLogger(watch.__name__)
    monkeypatch.setattr(watch_logger, "propagate", False)
    watch_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=watch_logger.name)
    yield caplog
    watch_logger.removeHandler(caplog.handler)


def _pushed_calls(pushed: list[dict]) -> list[tuple[str, str]]:
    return [(m["metadata"]["ws_id"], m["payload"]["request_id"]) for m in pushed
            if m["payload"]["event_type"] == "chat.client_tool_request"]


def test_a_waiting_conversation_no_client_shows_is_answered_as_gone_only_after_the_grace(clock, gone) -> None:
    async def scenario() -> list:
        pending_call("session-1", "call-1")
        pending_call("session-1", "call-2")
        pending_question("session-1", "approve-1")  # the user still answers it
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS - 1)
        await sweep(clock)
        within_grace = list(gone)
        clock.advance(1)
        await sweep(clock)
        return within_grace

    assert asyncio.run(scenario()) == []
    assert gone == [("session-1", "web", "user-1", ["call-1", "call-2"])]


def test_a_client_that_attaches_within_the_grace_gets_the_calls_and_none_is_answered_as_gone(clock, gone) -> None:
    async def scenario() -> list[str]:
        pending_call("session-1", "call-1")
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS - 1)
        attached = await answer_async(request())
        for _ in range(3):
            clock.advance(CLIENT_GONE_GRACE_SECONDS)
            await sweep(clock)
        return [event["request_id"] for event in attached["payload"]["pending"]]

    assert asyncio.run(scenario()) == ["call-1"]
    assert gone == []


def test_an_attachment_that_is_not_renewed_lapses_tells_the_others_and_leaves_its_calls_to_the_grace(
    clock, gone,
) -> None:
    pushed: list[dict] = []

    async def push(message) -> None:
        pushed.append(message)

    async def client_gone(session_id, channel_id, owner_id, pending) -> None:
        gone.append((session_id, channel_id, owner_id, [p["request_id"] for p in pending]))

    give_access(client_gone=client_gone, push=push)

    async def scenario() -> list:
        assert (await answer_async(request()))["ok"] is True
        budget = await answer_async(request(session="session-2", socket="ws-2", resource={**RESOURCE, "key": "b"}))
        pending_call("session-1", "call-1")
        clock.advance(LEASE_SECONDS - 1)
        await sweep(clock)
        assert (await answer_async(renew(budget["payload"]["attachment_id"], session="session-2", socket="ws-2")))[
            "ok"] is True
        clock.advance(1)
        pushed.clear()
        await sweep(clock)  # ws-1 lapses: from now on nothing shows session-1
        assert [c.ws_id for c in get_attachment_directory().connections()] == ["ws-2"]
        clock.advance(CLIENT_GONE_GRACE_SECONDS - 1)
        await sweep(clock)
        within_grace = list(gone)
        clock.advance(1)
        await sweep(clock)
        return within_grace

    assert asyncio.run(scenario()) == []
    assert gone == [("session-1", "web", "user-1", ["call-1"])]
    assert [(m["metadata"]["ws_id"], m["payload"]["event_type"]) for m in pushed] == [
        ("ws-2", "client.attachments_changed")]


def test_a_call_answered_meanwhile_is_not_answered_as_gone_and_a_new_pause_gets_a_full_grace(clock, gone) -> None:
    async def scenario() -> list:
        pending_call("session-1", "call-1")
        await sweep(clock)
        answered("session-1", "call-1")  # its result arrived
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)
        pending_call("session-1", "call-2")  # the next turn asks again
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS - 1)
        await sweep(clock)
        within_grace = list(gone)
        clock.advance(1)
        await sweep(clock)
        return within_grace

    assert asyncio.run(scenario()) == []
    assert gone == [("session-1", "web", "user-1", ["call-2"])]


def test_a_socket_that_switches_conversations_leaves_the_old_one_to_the_grace(clock, gone) -> None:
    async def scenario() -> None:
        assert (await answer_async(request()))["ok"] is True
        pending_call("session-1", "call-1")
        assert (await answer_async(request(session="session-2", resource={**RESOURCE, "key": "new"})))["ok"]
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)

    asyncio.run(scenario())

    assert gone == [("session-1", "web", "user-1", ["call-1"])]


def test_a_client_re_attaching_gets_what_its_conversation_s_pause_still_asks_with_the_attach() -> None:
    pending_question("session-1", "approve-1")
    pending_call("session-1", "call-1")
    pending_call("session-2", "call-2")
    pushed: list[dict] = []
    attached = asyncio.run(answer_async(request(socket="ws-9"), pushed))

    assert attached["ok"] is True
    assert [(event["event_type"], event["interrupt_id"]) for event in attached["payload"]["pending"]] == [
        ("chat.ask_user_question", "interrupt-approve-1"),
        ("chat.client_tool_request", "interrupt-call-1"),
    ]
    assert [m["payload"]["event_type"] for m in pushed] == ["client.attachments_changed"]


def test_reading_a_pause_before_the_server_set_up_conversation_access_says_so(monkeypatch) -> None:
    monkeypatch.setattr(watch, "_access", None)

    with pytest.raises(RuntimeError, match="set_conversation_access"):
        asyncio.run(watch.pending_interactions("session-1"))


def test_a_client_that_attaches_while_the_calls_are_answered_as_gone_does_not_get_them(clock) -> None:
    answering, claimed = asyncio.Event(), asyncio.Event()
    gone: list[list[str]] = []

    async def claim_slowly(session_id, channel_id, owner_id, pending) -> None:
        answering.set()
        await claimed.wait()
        gone.append([p["request_id"] for p in pending])
        PAUSED.pop(session_id, None)

    give_access(client_gone=claim_slowly)

    async def scenario() -> tuple[list[str], list[str]]:
        pending_call("session-1", "call-1")
        await watch.sweep_once(clock.now)
        await watch.sweep_once(clock.advance(CLIENT_GONE_GRACE_SECONDS))  # the answer runs in the background
        await asyncio.wait_for(answering.wait(), 2)
        reattached = await answer_async(request())
        await watch.sweep_once(clock.advance(CLIENT_GONE_GRACE_SECONDS))  # nor does a later sweep answer it twice
        claimed.set()
        await asyncio.gather(*watch._answers)
        return [event["request_id"] for event in reattached["payload"]["pending"]], [
            event["request_id"] for event in await watch.pending_interactions("session-1")]

    assert asyncio.run(scenario()) == ([], [])
    assert gone == [["call-1"]]


def test_a_slow_client_gone_answer_does_not_hold_up_the_sweep_of_other_conversations(clock) -> None:
    release, session_2_answered = asyncio.Event(), asyncio.Event()
    gone: list[str] = []

    async def session_1_slowly(session_id, channel_id, owner_id, pending) -> None:
        if session_id == "session-1":
            await release.wait()
        else:
            session_2_answered.set()
        gone.append(session_id)

    give_access(client_gone=session_1_slowly)

    async def scenario() -> list[str]:
        pending_call("session-1", "call-1")
        await watch.sweep_once(clock.now)
        await watch.sweep_once(clock.advance(CLIENT_GONE_GRACE_SECONDS))
        pending_call("session-2", "call-2")
        await watch.sweep_once(clock.now)
        await asyncio.wait_for(watch.sweep_once(clock.advance(CLIENT_GONE_GRACE_SECONDS)), 2)
        await asyncio.wait_for(session_2_answered.wait(), 2)
        before_release = list(gone)
        release.set()
        await asyncio.gather(*watch._answers)
        return before_release

    assert asyncio.run(scenario()) == ["session-2"]
    assert gone == ["session-2", "session-1"]


@pytest.mark.parametrize("attached_meanwhile", [False, True])
def test_a_failed_client_gone_answer_keeps_the_calls_waiting(clock, attached_meanwhile, watch_log) -> None:
    held: dict = {}
    failures: list[str] = []
    pushed: list[dict] = []

    async def fail(session_id, channel_id, owner_id, pending) -> None:
        held["answering"].set()
        await held["fail"].wait()
        failures.append(session_id)
        raise RuntimeError("the paused agent could not be rebuilt")

    async def push(message) -> None:
        pushed.append(message)

    give_access(client_gone=fail, push=push)

    async def scenario() -> None:
        held.update(answering=asyncio.Event(), fail=asyncio.Event())
        pending_call("session-1", "call-1")
        await watch.sweep_once(clock.now)
        await watch.sweep_once(clock.advance(CLIENT_GONE_GRACE_SECONDS))
        await asyncio.wait_for(held["answering"].wait(), 2)
        if attached_meanwhile:
            assert (await answer_async(request(socket="ws-2")))["payload"]["pending"] == []
        held["fail"].set()
        await asyncio.gather(*watch._answers)
        await sweep(clock)  # another grace starts, or the client attached meanwhile has the calls
        await sweep(clock)

    asyncio.run(scenario())

    assert failures == ["session-1"]
    assert "answering the calls of unattended session-1 failed" in watch_log.text
    assert [e["request_id"] for e in PAUSED["session-1"]] == ["call-1"]
    assert _pushed_calls(pushed) == ([("ws-2", "call-1")] if attached_meanwhile else [])


def test_a_conversation_whose_pause_cannot_be_read_does_not_stop_the_sweep(clock, gone, watch_log) -> None:
    async def session_2_unreadable(session_id: str) -> list[dict]:
        if session_id == "session-2":
            raise RuntimeError("permission_session_cleanup_pending")
        return await read_paused(session_id)

    async def client_gone(session_id, channel_id, owner_id, pending) -> None:
        gone.append(session_id)

    give_access(pending=session_2_unreadable, client_gone=client_gone)

    async def scenario() -> None:
        pending_call("session-2", "call-2")
        pending_call("session-1", "call-1")
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)

    asyncio.run(scenario())

    assert gone == ["session-1"]
    assert "sweeping session-2 failed" in watch_log.text


@pytest.mark.parametrize("refusal", [ClientToolAnswerMismatch("no paused call waits on these results"),
                                     NoControlTargetError("session has no active execution")])
def test_a_client_gone_answer_the_client_s_own_answer_overtook_is_logged_as_expected(
    refusal, clock, watch_log,
) -> None:
    async def overtaken(session_id, channel_id, owner_id, pending) -> None:
        raise refusal

    give_access(client_gone=overtaken)

    async def scenario() -> None:
        pending_call("session-1", "call-1")
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)

    asyncio.run(scenario())

    logged = [r for r in watch_log.records if r.name == watch.__name__]
    assert [r.levelname for r in logged] == ["INFO"]
    assert "session-1" in logged[0].getMessage()


def test_an_attach_whose_pause_cannot_be_read_is_refused_and_its_calls_wait_out_the_grace(clock, gone) -> None:
    reads = {"fail": True}

    async def read_once_failing(session_id: str) -> list[dict]:
        if reads.pop("fail", False):
            raise RuntimeError("permission_session_cleanup_pending")
        return await read_paused(session_id)

    async def client_gone(session_id, channel_id, owner_id, pending) -> None:
        gone.append(session_id)

    give_access(pending=read_once_failing, client_gone=client_gone)
    pending_call("session-1", "call-1")

    async def scenario() -> dict:
        refused = await answer_async(request())
        assert get_attachment_directory().connection_for_session("session-1") is None
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)
        return refused

    refused = asyncio.run(scenario())

    assert refused["ok"] is False and refused["payload"]["code"] == "UNAVAILABLE"
    assert gone == ["session-1"]


@pytest.mark.parametrize("taken", ["conversation", "resource"])
def test_a_refused_switch_does_not_take_back_what_another_socket_attached_meanwhile(clock, gone, taken) -> None:
    held: dict = {}

    async def session_2_read_slowly_then_fails(session_id: str) -> list[dict]:
        if session_id == "session-2":
            held["reading"].set()
            await held["release"].wait()
            raise RuntimeError("adapter rebuild failed")
        return await read_paused(session_id)

    async def client_gone(session_id, channel_id, owner_id, pending) -> None:
        gone.append(session_id)

    give_access(pending=session_2_read_slowly_then_fails, client_gone=client_gone)
    budget = {**RESOURCE, "key": "budget", "display_name": "Budget.pptx"}

    async def scenario() -> list[tuple[str, str, str]]:
        held.update(reading=asyncio.Event(), release=asyncio.Event())
        assert (await answer_async(request()))["ok"] is True  # ws-1: Roadmap shows session-1
        pending_call("session-1", "call-1")
        switching = asyncio.create_task(answer_async(request(session="session-2")))
        await asyncio.wait_for(held["reading"].wait(), 2)
        if taken == "conversation":  # another resource is attached to session-1
            meanwhile = request(session="session-1", socket="ws-2", resource=budget)
        else:  # Roadmap reopened on another socket, showing another conversation
            meanwhile = request(session="session-3", socket="ws-2")
        assert (await answer_async(meanwhile))["ok"] is True
        held["release"].set()
        assert (await switching)["ok"] is False
        listed = [(c.ws_id, c.display_name, c.session_id) for c in get_attachment_directory().connections()]
        await sweep(clock)
        clock.advance(CLIENT_GONE_GRACE_SECONDS)
        await sweep(clock)
        return listed

    listed = asyncio.run(scenario())
    if taken == "conversation":
        assert listed == [("ws-2", "Budget.pptx", "session-1")]
        assert gone == []
    else:
        assert listed == [("ws-2", RESOURCE["display_name"], "session-3")]
        assert gone == ["session-1"]  # no client is attached to it any more


def test_overlapping_client_gone_answers_of_one_conversation_each_hold_back_their_own_calls() -> None:
    releases = {"call-1": asyncio.Event(), "call-2": asyncio.Event()}

    async def answer_slowly(session_id, channel_id, owner_id, pending) -> None:
        await releases[pending[0]["request_id"]].wait()

    give_access(client_gone=answer_slowly)

    async def scenario() -> list[str]:
        first, second = pending_call("session-1", "call-1"), pending_call("session-1", "call-2")
        answering = []
        for call in (first, second):
            held_back = frozenset({call["request_id"]})
            watch._answering_client_gone["session-1"] = held_back
            answering.append(asyncio.create_task(watch._answered_as_gone("session-1", [call], held_back)))
        await asyncio.sleep(0.05)
        releases["call-1"].set()
        assert await answering[0] is True
        still_asked = [event["request_id"] for event in await watch.pending_interactions("session-1")]
        releases["call-2"].set()
        assert await answering[1] is True
        return still_asked

    assert asyncio.run(scenario()) == ["call-1"]  # call-2 is still being answered as gone


def test_the_server_starts_the_sweep_once_and_stopping_it_ends_the_sweep() -> None:
    async def scenario() -> tuple[bool, bool]:
        start_client_attachment_sweep()
        sweeper = watch._sweeper
        start_client_attachment_sweep()
        same = watch._sweeper is sweeper
        close_client_attachments()
        await asyncio.sleep(0)
        return same, sweeper.cancelled()

    assert asyncio.run(scenario()) == (True, True)
