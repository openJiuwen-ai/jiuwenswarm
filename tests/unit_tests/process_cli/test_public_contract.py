# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public schema, compatibility, diagnostics and discovery acceptance gates."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from jiuwenswarm.channels.process_cli.duplex_protocol import (
    DuplexProtocolError,
    decode_control,
)
from jiuwenswarm.channels.process_cli.machine_io import (
    MachineInputError,
    OneShotWriter,
    read_run_input,
)
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotEvent,
    OneShotRunInput,
    OneShotRunResult,
    RuntimeErrorInfo,
)
from jiuwenswarm.channels.process_cli.protocol.capabilities import protocol_capabilities
from jiuwenswarm.channels.process_cli.protocol.contracts import public_event_payload
from jiuwenswarm.channels.process_cli.protocol.schema import protocol_schema
from jiuwenswarm.runtime.events import RuntimeEvent

ROOT = Path(__file__).resolve().parents[3]


def test_published_schema_is_valid_and_matches_generator():
    spec = importlib.util.spec_from_file_location(
        "schema_generator", ROOT / "scripts/generate_process_cli_schema.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    schema = protocol_schema()
    Draft202012Validator.check_schema(schema)
    assert schema == generator.build_schema()
    caps = protocol_capabilities()
    assert set(caps["run_fields"]) == set(OneShotRunInput(input="hello").to_dict())
    assert caps["features"]["empty_tools"] and caps["protocol_revision"] == 1


@pytest.mark.parametrize(
    "event_type,payload",
    [
        ("chat.delta", {"content": "hello"}),
        ("chat.final", {"content": "world", "final_mode": "patch_segment"}),
        (
            "chat.tool_call",
            {
                "tool_call": {
                    "name": "read_file",
                    "tool_call_id": "call-1",
                    "arguments": '{"file_path":"a.txt"}',
                }
            },
        ),
        (
            "chat.tool_result",
            {
                "tool_name": "read_file",
                "tool_call_id": "call-1",
                "result": "value",
                "success": True,
            },
        ),
        (
            "host_tool.requested",
            {"call_id": "call-2", "name": "lookup", "arguments": {"key": "a"}},
        ),
        (
            "interaction.requested",
            {
                "interaction_id": "interaction-1",
                "interaction": {
                    "source": "permission_interrupt",
                    "questions": [
                        {
                            "question": "Approve?",
                            "card_id": "card-1",
                            "options": [
                                {"label": "Allow", "value": "allow_once"},
                                {"label": "Reject", "value": "reject"},
                            ],
                        }
                    ],
                },
            },
        ),
    ],
)
def test_real_writer_critical_events_match_public_schema_without_mutating_runtime(
    event_type, payload
):
    original = deepcopy(payload)
    output = io.StringIO()
    writer = OneShotWriter(output, request_id="outer")
    writer.session_id = "session"
    writer.write_event(
        RuntimeEvent(
            "internal",
            "process_cli",
            "session",
            payload={"event_type": event_type, **payload},
        )
    )
    record = json.loads(output.getvalue())
    Draft202012Validator(protocol_schema()).validate(record)
    assert payload == original
    assert record["payload"].items() >= payload.items()
    if event_type == "interaction.requested":
        assert record["payload"]["kind"] == "permission"
        assert record["payload"]["questions"][0]["options"][0]["value"] == "allow_once"
        assert not record["payload"]["questions"][0]["allow_custom_input"]


@pytest.mark.parametrize(
    "overrides,field,reason",
    [
        ({"max_turns": "secret-value"}, "/max_turns", "invalid_type"),
        ({"max_turns": 0}, "/max_turns", "invalid_value"),
        (
            {"agent": {"name": "test", "instructions": "hello", "tools": [42]}},
            "/agent/tools/0",
            "invalid_type",
        ),
        (
            {"permissions": {"tools": {"write_file": "bad"}}},
            "/permissions/tools/write_file",
            "invalid_value",
        ),
        ({"secret-unknown-name": "secret-value"}, "/", "unknown_field"),
    ],
)
def test_field_diagnostics_locate_problem_without_echoing_values(
    overrides, field, reason
):
    value = {
        "schema_version": "0.1",
        "type": "run",
        "request_id": "run",
        "input": "hello",
        **overrides,
    }
    with pytest.raises(MachineInputError) as caught:
        read_run_input("-", stdin=io.BytesIO(json.dumps(value).encode()))
    assert caught.value.details["field"] == field
    assert caught.value.details["reason"] == reason
    diagnostic = str(caught.value) + json.dumps(caught.value.details)
    assert "secret-value" not in diagnostic and "secret-unknown-name" not in diagnostic


@pytest.mark.parametrize("operation", ["protocol.capabilities", "protocol.schema"])
def test_protocol_discovery_starts_without_runtime_or_configuration(
    tmp_path, operation
):
    blocker = """
import importlib.abc, sys
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("jiuwenswarm.runtime", "openjiuwen")):
            raise AssertionError("Runtime imported for protocol discovery")
sys.meta_path.insert(0, BlockRuntime())
from jiuwenswarm.channels.process_cli.main import main
sys.argv = ["cli", "--query-json", "-"]
raise SystemExit(main())
"""
    document = {
        "schema_version": "0.1",
        "type": "query",
        "request_id": "q",
        "operation": operation,
    }
    run = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=ROOT,
        input=json.dumps(document).encode() + b"\n",
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    result = json.loads(run.stdout)
    assert result["status"] == "completed" and result["session_id"] is None
    Draft202012Validator(protocol_schema()).validate(result)
    assert result["data"] == (
        protocol_capabilities()
        if operation.endswith("capabilities")
        else protocol_schema()
    )


def test_output_decoders_accept_additive_fields_and_unknown_observation_events():
    event = OneShotEvent.from_dict(
        {
            "schema_version": "0.1",
            "type": "event",
            "sequence": 0,
            "request_id": "run",
            "event_type": "future.observation",
            "payload": {},
            "future_field": True,
        }
    )
    assert event.event_type == "future.observation"
    result = OneShotRunResult.from_dict(
        {
            "schema_version": "0.1",
            "type": "result",
            "sequence": 1,
            "request_id": "run",
            "session_id": "session",
            "status": "completed",
            "exit_code": 0,
            "future_field": True,
        }
    )
    assert result.status == "completed"
    assert (
        RuntimeErrorInfo.from_dict(
            {"code": "FUTURE_ERROR", "message": "failed", "future_field": True}
        ).code
        == "FUTURE_ERROR"
    )


@pytest.mark.parametrize(
    "answer,field",
    [
        ({"question": 42}, "/answers/0/question"),
        ({"custom_input": None}, "/answers/0/custom_input"),
        ({"selected_options": [42]}, "/answers/0/selected_options/0"),
        ({"card_id": " "}, "/answers/0/card_id"),
    ],
)
def test_invalid_public_answer_fields_have_nested_diagnostics(answer, field):
    document = {
        "schema_version": "0.1",
        "type": "answer",
        "request_id": "run",
        "session_id": "session",
        "interaction_id": "interaction",
        "answers": [answer],
    }
    with pytest.raises(DuplexProtocolError) as caught:
        decode_control(json.dumps(document).encode())
    assert caught.value.details["field"] == field
    assert caught.value.request_id == "run"


@pytest.mark.parametrize(
    "arguments", ['{"value": NaN}', '{"value": 1e999}', "invalid-json"]
)
def test_unparseable_tool_arguments_remain_observable_without_invalid_json(arguments):
    payload = {"tool_call": {"name": "lookup", "arguments": arguments}}
    public = public_event_payload("chat.tool_call", payload)
    assert public["tool"]["arguments"] is None
    assert public["tool_call"]["arguments"] == arguments
    json.dumps(public, allow_nan=False)
