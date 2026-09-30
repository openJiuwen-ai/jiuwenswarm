# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``ToolIdempotencyRail`` (A15).

The rail answers a repeated tool call at the *tool-call boundary*:
``AbilityManager._railed_execute_single_tool_call`` consults
``ctx.extra["_skip_tool"]`` after the ``BEFORE_TOOL_CALL`` hooks, so a rail that
sets it plus ``inputs.tool_result`` / ``inputs.tool_msg`` replaces the
downstream execution and its payload. Every test below therefore counts
**downstream calls** — one downstream call is one real side effect (a duplicate
delivery, a duplicate board read plus its rendering, a duplicate file body in
the next model request).

What is deliberately *not* claimed here: the model round trip that produced the
call is already paid for when the rail sees it. ``before_model_call`` only
changes what the model does in the following iteration, so no assertion in this
file may treat it as a saving inside the current iteration.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.plugins.tool_idempotency_rail import (
    _SKIP_MARKER,
    DEFAULT_VIEW_TASK_TTL_SECONDS,
    MAX_HASH_BYTES,
    ToolIdempotencyRail,
    normalize_snapshot,
    sha256_file,
)


class Boundary:
    """Mirror of ``AbilityManager._railed_execute_single_tool_call``.

    Runs the before hooks, honours ``_skip_tool`` by returning the cached pair
    *without* calling downstream, otherwise calls downstream and then runs the
    after hooks. The after hooks also run on the short-circuited path, exactly
    as in the framework (``_skip_tool`` is popped by the decorated executor),
    which is what proves the rail does not record a cache entry twice.
    """

    def __init__(self, rails, downstream, session_id: str = "sess-1") -> None:
        self.rails = list(rails)
        self.downstream = downstream
        self.session = SimpleNamespace(session_id=session_id)
        self.downstream_calls: list[tuple[str, dict]] = []

    async def _run(self, hook: str, ctx: AgentCallbackContext) -> None:
        for rail in self.rails:
            await getattr(rail, hook)(ctx)

    async def call(self, name: str, args, call_id: str = "call-1"):
        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        call = ToolCall(id=call_id, type="function", name=name, arguments=raw)
        inputs = ToolCallInputs(tool_call=call, tool_name=name, tool_args=raw)
        ctx = AgentCallbackContext(
            agent=None, inputs=inputs, session=self.session, extra={}
        )

        await self._run("before_tool_call", ctx)
        if ctx.extra.pop("_skip_tool", None):
            skipped = True
            result, message = ctx.inputs.tool_result, ctx.inputs.tool_msg
            await self._run("after_tool_call", ctx)
        else:
            skipped = False
            self.downstream_calls.append((name, parsed))
            result, message = self.downstream(name, parsed)
            inputs.tool_result = result
            inputs.tool_msg = message
            await self._run("after_tool_call", ctx)
        return SimpleNamespace(
            skipped=skipped,
            result=result,
            message=message,
            content=getattr(message, "content", "") or "",
            extra=ctx.extra,
        )


class DenyRail:
    """Stands in for a security / interrupt rail that already ruled."""

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = {"denied": "by security rail"}
        ctx.inputs.tool_msg = ToolMessage(content="denied", tool_call_id="")

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """No-op; the hook still fires on the short-circuited path."""


def _send_downstream(calls: list) -> Callable[..., Any]:
    def _fn(name: str, args: dict):
        calls.append((name, args))
        to = args.get("to", "")
        return (
            {"type": "message", "to": to},
            ToolMessage(
                content=f"Message Already sent from team-leader to {to} Success",
                tool_call_id="",
            ),
        )

    return _fn


def _ok_downstream(calls: list, body: str = "ok") -> Callable[..., Any]:
    def _fn(name: str, args: dict):
        calls.append((name, args))
        return {"ok": True}, ToolMessage(content=body, tool_call_id="")

    return _fn


@pytest.fixture
def clock():
    state = {"now": 1000.0}

    def _now() -> float:
        return state["now"]

    _now.state = state
    return _now


# -- send_message ---------------------------------------------------------------


async def test_repeated_send_message_is_served_from_cache_once():
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _send_downstream(calls))
    args = {"to": "member-1", "content": "please report progress"}

    first = await boundary.call("send_message", args, call_id="call-1")
    second = await boundary.call("send_message", args, call_id="call-2")

    assert first.skipped is False
    assert second.skipped is True
    assert len(calls) == 1, "a duplicate delivery must not be executed twice"
    assert second.content == first.content
    assert rail.skipped_calls == 1


async def test_cached_reply_addresses_the_current_tool_call():
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _send_downstream(calls))
    args = {"to": "member-1", "content": "ping"}

    await boundary.call("send_message", args, call_id="call-1")
    second = await boundary.call("send_message", args, call_id="call-2")

    assert second.skipped is True
    assert second.message.tool_call_id == "call-2", (
        "a replayed ToolMessage must carry the current call id, otherwise the "
        "provider rejects the conversation"
    )


async def test_send_message_args_may_arrive_as_a_json_string():
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _send_downstream(calls))
    raw = json.dumps({"to": "member-1", "content": "ping"})

    await boundary.call("send_message", raw, call_id="call-1")
    second = await boundary.call("send_message", raw, call_id="call-2")

    assert second.skipped is True
    assert len(calls) == 1


async def test_distinct_recipient_or_content_is_not_deduped():
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _send_downstream(calls))

    await boundary.call("send_message", {"to": "m1", "content": "ping"}, "call-1")
    other_to = await boundary.call(
        "send_message", {"to": "m2", "content": "ping"}, "call-2"
    )
    other_body = await boundary.call(
        "send_message", {"to": "m1", "content": "ping again"}, "call-3"
    )

    assert other_to.skipped is False
    assert other_body.skipped is False
    assert len(calls) == 3


# -- read_file ------------------------------------------------------------------


async def test_third_identical_read_file_is_short_circuited(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], lambda name, args: (
        calls.append((name, args)),
        ({"content": target.read_text(encoding="utf-8")},
         ToolMessage(content=target.read_text(encoding="utf-8"), tool_call_id="")),
    )[1])
    args = {"file_path": str(target)}

    await boundary.call("read_file", args, call_id="call-1")
    await boundary.call("read_file", args, call_id="call-2")
    third = await boundary.call("read_file", args, call_id="call-3")

    assert third.skipped is True
    assert len(calls) == 2, "the first two reads are served normally"
    assert third.content.startswith("[unchanged]")
    assert str(target) in third.content
    assert third.message.tool_call_id == "call-3"


async def test_read_file_hash_change_is_not_served_stale(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("v1\n", encoding="utf-8")
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], lambda name, args: (
        calls.append((name, args)),
        ({"content": target.read_text(encoding="utf-8")},
         ToolMessage(content=target.read_text(encoding="utf-8"), tool_call_id="")),
    )[1])
    args = {"file_path": str(target)}

    for i in range(3):
        await boundary.call("read_file", args, call_id=f"call-{i + 1}")
    target.write_text("v2\n", encoding="utf-8")
    after_edit = await boundary.call("read_file", args, call_id="call-4")

    assert after_edit.skipped is False, (
        "changed bytes are a different key; the rule never serves stale content"
    )
    assert after_edit.content == "v2\n"
    assert len(calls) == 3


def test_sha256_file_refuses_what_it_cannot_afford(tmp_path):
    small = tmp_path / "small.txt"
    small.write_bytes(b"abc")
    assert sha256_file(str(small)) == hashlib.sha256(b"abc").hexdigest()

    assert sha256_file(str(tmp_path / "missing.txt")) is None

    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"x" * (MAX_HASH_BYTES + 1))
    assert sha256_file(str(huge)) is None, (
        "hashing an oversized file would cost more than the re-read it avoids"
    )


async def test_unhashable_reads_fall_through_to_the_real_tool(tmp_path):
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _ok_downstream(calls, body="real body"))

    missing_args = {"file_path": str(tmp_path / "missing.txt")}
    for i in range(3):
        outcome = await boundary.call("read_file", missing_args, f"call-{i}")
        assert outcome.skipped is False

    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"x" * (MAX_HASH_BYTES + 1))
    huge_args = {"file_path": str(huge)}
    for i in range(3):
        outcome = await boundary.call("read_file", huge_args, f"huge-{i}")
        assert outcome.skipped is False

    assert len(calls) == 6, "the tool's own error handling must stay in charge"


# -- view_task ------------------------------------------------------------------


async def test_view_task_cache_is_off_by_default():
    assert DEFAULT_VIEW_TASK_TTL_SECONDS == 0.0
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _ok_downstream(calls, body="task-a (T)"))

    for i in range(4):
        outcome = await boundary.call("view_task", {"action": "list"}, f"call-{i + 1}")
        assert outcome.skipped is False

    assert len(calls) == 4, (
        "with the board rule disabled every poll must reach the board"
    )


async def test_view_task_serves_only_a_known_stable_board(clock):
    calls: list = []
    rail = ToolIdempotencyRail(view_task_ttl_seconds=30, clock=clock)
    boundary = Boundary([rail], _ok_downstream(calls, body="task-a (T)"))
    args = {"action": "list"}

    first = await boundary.call("view_task", args, "call-1")
    second = await boundary.call("view_task", args, "call-2")
    third = await boundary.call("view_task", args, "call-3")

    assert first.skipped is False
    assert second.skipped is False, "stability is not known after a single poll"
    assert third.skipped is True, "the board was observed to stand still"
    assert len(calls) == 2

    clock.state["now"] += 31
    expired = await boundary.call("view_task", args, "call-4")

    assert expired.skipped is False, "the TTL is the backstop for unseen writes"
    assert len(calls) == 3


async def test_board_write_invalidates_the_snapshot(clock):
    calls: list = []
    rail = ToolIdempotencyRail(view_task_ttl_seconds=300, clock=clock)
    boundary = Boundary([rail], _ok_downstream(calls, body="task-a (T)"))

    for i in range(3):
        await boundary.call("view_task", {"action": "list"}, f"poll-{i}")
    cached = await boundary.call("view_task", {"action": "list"}, "poll-3")
    assert cached.skipped is True

    await boundary.call("complete_task", {"task_id": "t-1"}, "write-1")
    after_write = await boundary.call("view_task", {"action": "list"}, "poll-4")

    assert after_write.skipped is False, (
        "a member that wrote to the board must not be served its own snapshot"
    )


async def test_point_lookup_is_never_cached(clock):
    calls: list = []
    rail = ToolIdempotencyRail(view_task_ttl_seconds=300, clock=clock)
    boundary = Boundary([rail], _ok_downstream(calls, body="task-a (T)"))
    args = {"action": "get", "task_id": "t-1"}

    await boundary.call("view_task", args, "call-1")
    second = await boundary.call("view_task", args, "call-2")

    assert second.skipped is False
    assert len(calls) == 2


# -- isolation and precedence ---------------------------------------------------


async def test_cache_never_crosses_sessions():
    calls: list = []
    rail = ToolIdempotencyRail()
    args = {"to": "member-1", "content": "ping"}

    first = Boundary([rail], _send_downstream(calls), session_id="sess-1")
    second = Boundary([rail], _send_downstream(calls), session_id="sess-2")

    await first.call("send_message", args, "call-1")
    outcome = await second.call("send_message", args, "call-2")

    assert outcome.skipped is False, (
        "a rail instance reused across sessions must not answer for another one"
    )
    assert len(calls) == 2


async def test_security_rail_decision_is_not_overridden():
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([DenyRail(), rail], _send_downstream(calls))
    args = {"to": "member-1", "content": "ping"}

    first = await boundary.call("send_message", args, "call-1")
    second = await boundary.call("send_message", args, "call-2")

    assert first.skipped is True and first.content == "denied"
    assert second.content == "denied", "the denied pair must not be cached as a result"
    assert rail.skipped_calls == 0, "the rail must not claim a decision it did not make"
    assert calls == []


async def test_failed_call_is_not_cached():
    calls: list = []
    rail = ToolIdempotencyRail()
    state = {"fail": True}

    def _downstream(name: str, args: dict):
        calls.append((name, args))
        if state["fail"]:
            raise RuntimeError("downstream failed")
        return {"ok": True}, ToolMessage(content="ok", tool_call_id="")

    boundary = Boundary([rail], _downstream)
    args = {"to": "member-1", "content": "ping"}

    with pytest.raises(RuntimeError):
        await boundary.call("send_message", args, "call-1")

    state["fail"] = False
    retry = await boundary.call("send_message", args, "call-2")

    assert retry.skipped is False, "a failed call must be retried for real"
    assert len(calls) == 2


# -- model-call nudge and helpers -----------------------------------------------


async def test_before_model_call_nudges_once_per_key():
    calls: list = []
    rail = ToolIdempotencyRail(nudge_after_hits=2)
    boundary = Boundary([rail], _send_downstream(calls))
    args = {"to": "member-1", "content": "ping"}

    for i in range(3):
        await boundary.call("send_message", args, f"call-{i + 1}")

    notes: list[str] = []
    ctx = SimpleNamespace(push_steering=notes.append, extra={})
    await rail.before_model_call(ctx)
    await rail.before_model_call(ctx)

    assert len(notes) == 1, "one steering note per hot key, not one per iteration"
    assert "send_message" in notes[0]
    assert rail.nudges_sent == 1


def test_normalize_snapshot_ignores_the_relative_time():
    """Stability is judged on the board, not on the clock.

    ``ViewTaskTool`` renders every list entry as
    ``#<id> [<status>] <title> (<assignee>) (<absolute> (<relative>))``
    (``openjiuwen/agent_teams/tools/tool_task.py``, list view), so between two
    polls of a board that stood still only the relative bucket differs.
    ``normalize_snapshot`` must erase exactly that rendering — and nothing
    else, or a moved board would compare equal.
    """
    from openjiuwen.agent_teams.timefmt import format_time_context

    transition_ms = 1_758_000_000_000

    def _board(now_ms: int, status: str = "in_progress") -> str:
        stamp = format_time_context(transition_ms, now_ms)
        return f"#1 [{status}] Write the intro (dev-1) ({stamp})"

    first = _board(transition_ms + 3 * 60_000)
    later = _board(transition_ms + 9 * 60_000)
    moved = _board(transition_ms + 9 * 60_000, status="done")

    assert "(3 分钟前)" in first and "(9 分钟前)" in later
    assert normalize_snapshot(first) == normalize_snapshot(later)
    assert normalize_snapshot(first) != normalize_snapshot(moved)


async def test_skip_breadcrumb_names_the_tool_and_call():
    calls: list = []
    rail = ToolIdempotencyRail()
    boundary = Boundary([rail], _send_downstream(calls))
    args = {"to": "member-1", "content": "ping"}

    await boundary.call("send_message", args, "call-1")
    second = await boundary.call("send_message", args, "call-2")

    assert second.extra[_SKIP_MARKER] == {
        "tool": "send_message",
        "tool_call_id": "call-2",
    }
