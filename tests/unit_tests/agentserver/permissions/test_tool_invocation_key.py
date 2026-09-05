from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
)


def test_wire_key_round_trip_is_exact_and_normalized() -> None:
    key = ToolInvocationKeyV1(
        invocation_id=" tiv_1 ",
        root_session_id=" session-1 ",
        request_id=" request-1 ",
        executor_kind="agent",
        execution_session_id=" agent-1 ",
        tool_call_id=" call-1 ",
    )

    assert ToolInvocationKeyV1.from_wire(key.to_wire()) == key
    assert key.invocation_id == "tiv_1"
    assert key.to_wire() == {
        "version": 1,
        "invocation_id": "tiv_1",
        "root_session_id": "session-1",
        "request_id": "request-1",
        "executor_kind": "agent",
        "execution_session_id": "agent-1",
        "tool_call_id": "call-1",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("request_id"),
        lambda value: value.update(extra="x"),
        lambda value: value.update(version=True),
        lambda value: value.update(version=2),
        lambda value: value.update(executor_kind="team"),
        lambda value: value.update(executor_kind="team_agent"),
        lambda value: value.update(executor_kind="subagent"),
        lambda value: value.update(executor_kind=[]),
        lambda value: value.update(executor_kind={}),
        lambda value: value.update(invocation_id=""),
    ],
)
def test_wire_key_rejects_partial_or_invalid_identity(mutate: object) -> None:
    value = ToolInvocationKeyV1(
        "tiv_1", "session-1", "request-1", "agent", "session-1", "call-1"
    ).to_wire()
    mutate(value)

    with pytest.raises(ValueError):
        ToolInvocationKeyV1.from_wire(value)


def test_wire_key_rejects_non_mapping() -> None:
    with pytest.raises(ValueError):
        ToolInvocationKeyV1.from_wire([])
