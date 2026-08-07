# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The identical-repeat detector, from the 2026-08-02 Git OOM incident.

The agent called one command nine times with byte-identical arguments and got a
byte-identical failure each time. The detector ladder had no rule that would
interrupt that: `generic_repeat` matches the signature but is WARNING-only at
any count, and a warning was a log line the model never saw. The rules that do
interrupt need 10 consecutive errors, 20 ping-pong rounds or 30 no-progress
calls -- and each attempt in that incident could take tens of gigabytes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.execution_guard import (
    CircuitBreakerConfig,
    CircuitBreakerRail,
)

COMMAND = "git log -1 --format=%ad --date=format:'%m月%d日'"
OOM = json.dumps(
    {"command": COMMAND, "exit_code": 128, "stdout": "",
     "stderr": "fatal: Out of memory, realloc failed"}
)


class _Ctx:
    def __init__(self, args: dict[str, Any], result: str) -> None:
        self.session = SimpleNamespace(session_id="sess-1")
        self.inputs = SimpleNamespace(
            tool_call=SimpleNamespace(name="bash", arguments=args),
            tool_name="bash",
            tool_args=args,
            tool_result=result,
            tool_msg=None,
        )
        self.exception = None
        # The rail resolves its session bucket through ctx.extra.
        self.extra: dict[str, Any] = {}
        self.steering: list[str] = []
        self.force_finish: dict | None = None

    def push_steering(self, msg: str) -> None:
        self.steering.append(msg)

    def request_force_finish(self, result: dict) -> None:
        self.force_finish = result


def _rail(threshold: int = 3, abort: int = 5) -> CircuitBreakerRail:
    return CircuitBreakerRail(
        CircuitBreakerConfig(
            identical_repeat_threshold=threshold,
            identical_repeat_abort_threshold=abort,
        ),
        language="en",
    )


async def _replay(rail: CircuitBreakerRail, times: int, result: str = OOM) -> list[_Ctx]:
    seen = []
    for _ in range(times):
        ctx = _Ctx({"command": COMMAND}, result)
        await rail.after_tool_call(ctx)
        seen.append(ctx)
    return seen


@pytest.mark.asyncio
async def test_the_warning_stage_does_not_end_the_run() -> None:
    """The incident's signature: same tool, same args, same failure.

    The threshold warns; only the abort threshold stops anything. The gap
    between the two is what a legitimate poller gets to survive.
    """
    contexts = await _replay(_rail(threshold=3), times=3)

    assert all(ctx.force_finish is None for ctx in contexts)


@pytest.mark.asyncio
async def test_the_warning_stage_appends_nothing_to_the_model_context() -> None:
    """Deliberate, and the reason is measured rather than stylistic.

    An earlier version pushed a steering message here so the warning would reach
    the model. Replaying the incident says that backfires: `push_steering` lands
    as a message *after* the failing tool result, and on the model that suffered
    the incident, appending anything there takes the failure out of the position
    it answers -- 10/10 recovery when the tool result is last, 8-in-20 repetition
    when a steering message follows it, with a neutral note doing it as reliably
    as an instruction to stop.

    So this pins the absence, not a behaviour. Without it, "make warnings reach
    the model" reads like an obvious improvement and would come back.
    """
    contexts = await _replay(_rail(threshold=2, abort=9), times=5)

    assert all(ctx.steering == [] for ctx in contexts)
    assert all(ctx.force_finish is None for ctx in contexts)


@pytest.mark.asyncio
async def test_the_abort_threshold_ends_the_run() -> None:
    """The incident reached nine. This ends it at five."""
    contexts = await _replay(_rail(threshold=3, abort=5), times=5)

    assert contexts[3].force_finish is None
    assert contexts[4].force_finish is not None
    assert "abort" in contexts[4].force_finish["output"].lower()


@pytest.mark.asyncio
async def test_the_run_survives_every_repeat_below_the_abort_threshold() -> None:
    """Nothing between the two stages ends the run, however many rounds it takes."""
    contexts = await _replay(_rail(threshold=3, abort=6), times=5)

    assert all(ctx.force_finish is None for ctx in contexts)


@pytest.mark.asyncio
async def test_polling_an_unchanged_result_is_not_cut_at_the_warning_threshold() -> None:
    """Checking the same unfinished build three times is identical, not a fault.

    This is why the low threshold steers instead of aborting: the signature of a
    deterministic failure and the signature of legitimate polling are the same
    for the first few rounds, and only one of them deserves to be killed.
    """
    contexts = await _replay(_rail(threshold=3, abort=5), times=4)
    assert all(ctx.force_finish is None for ctx in contexts)


@pytest.mark.asyncio
async def test_a_cut_run_is_reported_as_an_error_not_an_answer() -> None:
    """Reporting an abort as an answer is how a failed run looks like a success."""
    contexts = await _replay(_rail(threshold=2, abort=3), times=3)
    assert contexts[-1].force_finish["result_type"] == "error"


@pytest.mark.asyncio
async def test_the_abort_threshold_cannot_undercut_the_warning() -> None:
    """A misconfiguration must not turn the warning stage into a silent abort."""
    contexts = await _replay(_rail(threshold=4, abort=2), times=4)
    assert all(ctx.force_finish is None for ctx in contexts), (
        "an abort threshold below the warning must not abort at the warning count"
    )


@pytest.mark.asyncio
async def test_a_changing_result_is_progress_and_is_not_cut() -> None:
    """Identical arguments returning new output each time is still work."""
    rail = _rail(threshold=3)
    for index in range(5):
        ctx = _Ctx(
            {"command": "git log -1"},
            json.dumps({"exit_code": 0, "stdout": f"commit {index}", "stderr": ""}),
        )
        await rail.after_tool_call(ctx)
        assert ctx.force_finish is None


@pytest.mark.asyncio
async def test_changing_the_command_resets_the_streak() -> None:
    rail = _rail(threshold=3)
    await _replay(rail, times=2)

    other = _Ctx({"command": "git status"}, OOM)
    await rail.after_tool_call(other)
    assert other.force_finish is None

    resumed = _Ctx({"command": COMMAND}, OOM)
    await rail.after_tool_call(resumed)
    assert resumed.force_finish is None, "the streak restarted at the different command"


@pytest.mark.asyncio
async def test_the_threshold_never_drops_below_two() -> None:
    """One failure is not a loop; a misconfigured 1 must not cut the first call."""
    contexts = await _replay(_rail(threshold=1), times=1)
    assert contexts[0].force_finish is None


@pytest.mark.asyncio
async def test_no_warning_level_detector_writes_to_the_model_context() -> None:
    """Not just the repeat rule -- ping-pong warnings must stay log-only too.

    The measurement that condemned steering is about *where* a message lands
    (after the failing tool result), not about which detector produced it, so the
    ban has to cover every warning-level path rather than the one that was tested.
    """
    rail = CircuitBreakerRail(
        CircuitBreakerConfig(warning_threshold=2, identical_repeat_threshold=99),
        language="en",
    )
    for index in range(6):
        command = "git status" if index % 2 else "git log -1"
        ctx = _Ctx(
            {"command": command},
            json.dumps({"exit_code": 1, "stdout": "", "stderr": "same"}),
        )
        await rail.after_tool_call(ctx)
        assert ctx.steering == [], "warnings must not be appended to the context"
        assert ctx.force_finish is None, "a warning must not cut the run either"


# ---------------------------------------------------------------------------
# Not becoming a new way to break working agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_successes_are_never_aborted() -> None:
    """Polling looks exactly like the incident for the first few rounds.

    Five `git status` on a clean tree, or a readiness check against a service
    that is not up yet, are identical tool, arguments *and* result -- the whole
    signature. The only thing separating them from the incident is that they
    succeed, so the abort requires failure and the warning does not.
    """
    rail = _rail(threshold=2, abort=3)
    ok = json.dumps({"exit_code": 0, "stdout": "nothing to commit", "stderr": ""})
    seen = []
    for _ in range(8):
        ctx = _Ctx({"command": "git status"}, ok)
        await rail.after_tool_call(ctx)
        seen.append(ctx)

    assert all(ctx.force_finish is None for ctx in seen), (
        "a healthy poller must never be ended by the repeat rule"
    )


@pytest.mark.asyncio
async def test_a_failing_streak_still_aborts_after_an_earlier_success() -> None:
    """Guards the other side: requiring failure must not let a stuck run escape."""
    rail = _rail(threshold=2, abort=3)
    ctx = _Ctx({"command": COMMAND},
               json.dumps({"exit_code": 0, "stdout": "fine", "stderr": ""}))
    await rail.after_tool_call(ctx)
    assert ctx.force_finish is None

    contexts = await _replay(rail, times=3)
    assert contexts[-1].force_finish is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,abort", [(0, 0), (0, 5), (3, 0)])
async def test_zero_disables_the_stage_it_names(threshold: int, abort: int) -> None:
    """Zeroing a threshold must turn a stage off, not make it stricter.

    Without this, `identical_repeat_abort_threshold: 0` fell into
    `max(warn + 1, 0)` and aborted at 4 -- one round *earlier* than the shipped
    default. An operator disabling a guard must never get a tighter one.
    """
    contexts = await _replay(_rail(threshold=threshold, abort=abort), times=8)

    if abort <= 0:
        assert all(ctx.force_finish is None for ctx in contexts)
    if threshold <= 0 and abort <= 0:
        assert all(ctx.steering == [] for ctx in contexts)


def test_the_history_window_fits_every_detector() -> None:
    """A raised threshold must not become a rule that can never fire."""
    assert CircuitBreakerConfig(
        identical_repeat_abort_threshold=200
    ).history_size >= 200


# ---------------------------------------------------------------------------
# The detectors the `enabled` flip wakes up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interleaved_work_does_not_hide_the_repeat() -> None:
    """One `read_file` between attempts used to reset the count to zero.

    The incident happened to be nine strictly consecutive calls, so requiring
    that was enough for it -- but a model that writes a todo or reads a file
    between attempts is doing the same thing and was invisible. Calls to *other*
    tools are skipped; a different call to the *same* tool is real variation and
    still ends the streak.
    """
    rail = _rail(threshold=2, abort=3)
    for _ in range(3):
        failing = _Ctx({"command": COMMAND}, OOM)
        await rail.after_tool_call(failing)

        noise = _Ctx({"path": "README.md"}, json.dumps({"content": "x"}))
        noise.inputs.tool_name = "read_file"
        noise.inputs.tool_call.name = "read_file"
        await rail.after_tool_call(noise)

    assert failing.force_finish is not None, (
        "interleaved unrelated work must not hide an identical repeat"
    )


@pytest.mark.asyncio
async def test_the_skip_budget_is_per_gap_not_cumulative() -> None:
    """Two interleaved calls per attempt must still reach the abort.

    A budget spent once for the whole walk caps the streak at roughly the
    budget itself, so a loop with a couple of calls between attempts could
    warn forever and never abort -- the exact shape of a model that writes a
    todo and reads a file between retries. Runs at the shipped thresholds,
    because that is where the interaction bites.
    """
    rail = _rail()
    for _ in range(5):
        failing = _Ctx({"command": COMMAND}, OOM)
        await rail.after_tool_call(failing)

        for name, args in (("read_file", {"path": "README.md"}),
                           ("write_todo", {"todo": "retry"})):
            noise = _Ctx(args, json.dumps({"content": "x"}))
            noise.inputs.tool_name = name
            noise.inputs.tool_call.name = name
            await rail.after_tool_call(noise)

    assert failing.force_finish is not None, (
        "a per-gap budget spent cumulatively lets the abort never fire"
    )


@pytest.mark.asyncio
async def test_a_different_call_to_the_same_tool_still_ends_the_streak() -> None:
    """Varying the command is the behaviour the guard wants; never punish it."""
    rail = _rail(threshold=2, abort=3)
    await _replay(rail, times=2)

    other = _Ctx({"command": "git status"}, OOM)
    await rail.after_tool_call(other)
    again = _Ctx({"command": COMMAND}, OOM)
    await rail.after_tool_call(again)

    assert again.force_finish is None


@pytest.mark.asyncio
async def test_consecutive_failures_of_one_tool_are_cut_by_unknown_tool_repeat() -> None:
    """Pins a detector that was inert until this change enabled the breaker.

    The name is historical: it counts consecutive *erroring* calls to one tool,
    whether or not that tool exists. It matters now because the failure-payload
    change also made more paths count as errors, so this rule is strictly more
    sensitive than it was when its threshold was chosen.
    """
    rail = CircuitBreakerRail(
        CircuitBreakerConfig(unknown_tool_threshold=3, identical_repeat_threshold=0,
                             identical_repeat_abort_threshold=0),
        language="en",
    )
    seen = []
    for index in range(3):
        ctx = _Ctx(
            {"command": f"pytest -k case{index}"},
            json.dumps({"exit_code": 1, "stdout": "", "stderr": f"fail {index}"}),
        )
        await rail.after_tool_call(ctx)
        seen.append(ctx)

    assert seen[-1].force_finish is not None
    assert "failed on" in seen[-1].force_finish["output"], (
        "the message must not call a registered tool 'unknown'"
    )


@pytest.mark.asyncio
async def test_a_success_resets_the_consecutive_failure_streak() -> None:
    """Otherwise a long debugging session accumulates toward a forced stop."""
    rail = CircuitBreakerRail(
        CircuitBreakerConfig(unknown_tool_threshold=3, identical_repeat_threshold=0,
                             identical_repeat_abort_threshold=0),
        language="en",
    )
    for index in range(2):
        await rail.after_tool_call(_Ctx(
            {"command": f"pytest -k a{index}"},
            json.dumps({"exit_code": 1, "stdout": "", "stderr": f"x{index}"})))

    await rail.after_tool_call(_Ctx(
        {"command": "pytest -k passing"},
        json.dumps({"exit_code": 0, "stdout": "ok", "stderr": ""})))

    ctx = _Ctx({"command": "pytest -k b"},
               json.dumps({"exit_code": 1, "stdout": "", "stderr": "y"}))
    await rail.after_tool_call(ctx)
    assert ctx.force_finish is None
