# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A tool result that states ``ok: false`` is a failed call.

``ok`` with ``error`` beside it is how ``sdd_advance`` reports a refused stage
transition, and how the shipped skill scripts report a failed step.
``_infer_tool_result_error`` reads ``success``, ``is_error``, ``status`` and the
exit-code keys. Without ``ok``, such a result was emitted with no verdict on it,
and every reader of that event showed it as a call that worked.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.common.rail_state_machine import (
    RailStateMachineBase,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _infer_tool_result_error,
)


class _StreamSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def write_stream(self, event: object) -> None:
        self.events.append(event)


async def _emit(result: object) -> dict[str, object]:
    """Emit one tool result and return the payload a reader receives."""
    session = _StreamSession()
    await JiuSwarmStreamEventRail()._emit_tool_result(
        session, SimpleNamespace(name="sdd_advance", id="tc-1"), result
    )
    return session.events[0].payload["tool_result"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": False, "error": "invalid stage: 'nope'"}, True),
        ({"ok": False}, True),
        ({"ok": "false"}, True),
        ({"ok": True, "from": "init", "to": "analysis"}, False),
        ({"ok": "true"}, False),
        # Absent, and present as a value that names neither outcome.
        ({"stage": "analysis"}, None),
        ({"ok": None}, None),
        ({"ok": 1}, None),
    ],
)
def test_ok_is_read_only_when_it_states_an_outcome(
    payload: dict[str, object], expected: bool | None
) -> None:
    assert _infer_tool_result_error(payload) is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"success": False}, True),
        ({"success": True}, False),
        ({"success": 1}, None),
        ({"is_error": True}, True),
        ({"isError": True}, True),
        ({"status": "failed"}, True),
        ({"status": "complete"}, None),
        ({"exit_code": 0}, False),
        ({"exit_code": 2}, True),
        ({"returncode": "1"}, True),
        ({"stdout": "done"}, None),
        ({"data": {"success": False}}, True),
        ({"raw_output": {"exit_code": 1}}, True),
        ({"result": {"is_error": True}}, True),
        ("[ERROR] no such file", True),
        ("exit_code: 3", True),
        ("", None),
    ],
)
def test_the_other_conventions_are_unchanged(
    payload: object, expected: bool | None
) -> None:
    assert _infer_tool_result_error(payload) is expected


@pytest.mark.parametrize("key", ["data", "raw_output", "rawOutput", "result"])
def test_a_nested_ok_is_reached_like_every_other_convention(key: str) -> None:
    assert _infer_tool_result_error({key: {"ok": False, "error": "no"}}) is True
    assert _infer_tool_result_error({key: {"ok": True}}) is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # A key the function already read states the outcome, so it decides and
        # the verdict is the one it gave before ``ok`` was read at all.
        ({"success": True, "ok": False}, False),
        ({"success": False, "ok": True}, True),
        ({"is_error": True, "ok": True}, True),
        ({"status": "error", "ok": True}, True),
        ({"exit_code": 1, "ok": True}, True),
        ({"exit_code": 0, "ok": False}, False),
    ],
)
def test_an_explicit_verdict_elsewhere_still_decides(
    payload: dict[str, object], expected: bool
) -> None:
    assert _infer_tool_result_error(payload) is expected


@pytest.mark.asyncio
async def test_a_refused_stage_transition_is_emitted_as_a_failed_call() -> None:
    rail = RailStateMachineBase(rail_pkg_dir=Path("."), project_dir=Path("."))
    refusal = rail._handle_advance({"stage": "nope"})
    assert refusal["ok"] is False

    payload = await _emit(refusal)

    assert payload["success"] is False
    assert payload["status"] == "error"
    assert payload["is_error"] is True
    assert payload["raw_output"]["error"] == refusal["error"]


@pytest.mark.asyncio
async def test_a_call_that_worked_is_emitted_as_a_call_that_worked() -> None:
    payload = await _emit({"ok": True, "from": "init", "to": "analysis"})

    assert payload["success"] is True
    assert "status" not in payload
    assert "is_error" not in payload


@pytest.mark.asyncio
async def test_a_payload_without_ok_carries_no_verdict() -> None:
    payload = await _emit({"stage": "analysis", "status": "complete"})

    assert "success" not in payload
    assert "is_error" not in payload
