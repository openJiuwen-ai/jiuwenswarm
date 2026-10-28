# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run-scoped structured output, usage limits, and unattended permissions."""

import io
import json

import pytest

from jiuwenswarm.channels.process_cli.duplex_control import DuplexController, DuplexControlError
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.machine_policy import (
    RunLimits, parse_structured_output, validate_output_schema,
)
from jiuwenswarm.channels.process_cli.protocol import OneShotRunInput
from jiuwenswarm.runtime.events import RuntimeEvent


def _usage(cost):
    return RuntimeEvent(
        request_id="run", channel_id="process_cli", session_id="session",
        payload={"event_type": "chat.usage_metadata", "metadata": {
            "usage_metadata": {"total_cost": cost}}},
    )


def test_structured_output_must_match_schema():
    schema = {"type": "object", "required": ["answer"],
              "properties": {"answer": {"type": "integer"}},
              "additionalProperties": False}
    assert parse_structured_output('{"answer": 42}', schema) == {"answer": 42}
    for output in ('{"answer":"42"}', "```json\n{}\n```", '{"answer": 42} extra'):
        with pytest.raises(ValueError):
            parse_structured_output(output, schema)
    with pytest.raises(ValueError):
        validate_output_schema({"type": "object", "properties": {"x": {"type": "bad"}}})


def test_limits_fail_closed_when_cost_missing_and_track_calls():
    limits = RunLimits(max_turns=1, max_budget_usd=0.5)
    assert limits.observe(_usage(0.3)) is None
    assert limits.observe(_usage(0.3)) == "BUDGET_EXCEEDED"
    assert limits.model_calls == 2
    assert RunLimits(max_turns=None, max_budget_usd=1).observe(_usage(None)) == "BUDGET_METER_UNAVAILABLE"
    turns = RunLimits(max_turns=1, max_budget_usd=None)
    assert turns.observe(_usage(None)) is None
    assert turns.observe(_usage(None)) == "TURN_LIMIT_EXCEEDED"


def test_unattended_permission_uses_explicit_reject_only():
    writer = OneShotWriter(io.StringIO(), request_id="run")
    control = DuplexController(None, writer, unattended=True)
    card = {"source": "permission_interrupt", "questions": [
        {"options": [{"value": "allow"}, {"value": "reject"}]},
    ]}
    assert control._permission_rejection(card) == ({"selected_options": ["reject"]},)
    with pytest.raises(DuplexControlError):
        control._permission_rejection({**card, "source": "ask_user_interrupt"})
    with pytest.raises(DuplexControlError):
        control._permission_rejection({"source": "permission_interrupt", "questions": [
            {"options": [{"value": "allow"}]},
        ]})


def test_run_request_round_trip_new_options():
    request = OneShotRunInput.from_dict({
        "schema_version": "0.1", "type": "run", "input": "hello",
        "output_schema": {"type": "object"}, "max_turns": 2,
        "max_budget_usd": 0.01,
        "host_tools": [{"name": "lookup", "description": "Lookup a value",
                        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}}}],
    })
    assert OneShotRunInput.from_dict(json.loads(json.dumps(request.to_dict()))) == request
