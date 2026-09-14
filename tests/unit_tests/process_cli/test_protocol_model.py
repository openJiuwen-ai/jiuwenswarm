# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import protocol
from jiuwenswarm.channels.process_cli.protocol import (
    AgentSpec,
    OneShotEvent,
    OneShotRunInput,
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
    SingleAgentMode,
    WorkspaceSpec,
)
from jiuwenswarm.common.mode_matrix import SINGLE_AGENT_CANONICAL_MODES
from jiuwenswarm.runtime.agent_definition import RuntimeAgentDefinition
from jiuwenswarm.runtime.events import RuntimeEvent


PROJECT_ROOT = Path(__file__).parents[3]
CONTRACT_TYPES = (
    AgentSpec,
    WorkspaceSpec,
    OneShotRunInput,
    RuntimeErrorInfo,
    OneShotEvent,
    OneShotRunResult,
)


def _agent() -> AgentSpec:
    return AgentSpec(
        name="local_agent",
        instructions="  keep instruction whitespace  ",
        model="GLM-5.2",
        tools=("read_file",),
        skills=("review",),
        max_iterations=12,
    )


def test_agent_spec_maps_directly_to_transport_neutral_runtime_definition() -> None:
    protocol_agent = AgentSpec(
        name="local_agent",
        instructions="Keep this instruction.",
        tools=("*",),
    )

    runtime_agent = RuntimeAgentDefinition.from_mapping(protocol_agent.to_dict())

    assert runtime_agent.to_dict() == {
        **protocol_agent.to_dict(),
        "tools": "*",
    }


def test_protocol_public_surface_is_one_shot_and_single_agent_only() -> None:
    assert set(protocol.__all__) == {
        "AgentSpec",
        "CURRENT_SCHEMA_VERSION",
        "JsonObject",
        "JsonScalar",
        "JsonValue",
        "OneShotEvent",
        "OneShotRecord",
        "OneShotRunInput",
        "OneShotRunResult",
        "RunStatus",
        "RuntimeErrorInfo",
        "SUPPORTED_SCHEMA_VERSIONS",
        "SingleAgentMode",
        "WorkspaceSpec",
        "decode_jsonl",
        "encode_jsonl",
        "encode_jsonl_record",
        "is_schema_version_supported",
        "require_supported_schema_version",
        "validate_one_shot_records",
    }
    forbidden = {
        "SessionCreateInput",
        "SessionDescriptor",
        "SessionPersistence",
        "TurnInput",
        "TurnState",
        "InteractionResponse",
        "ToolResult",
    }
    assert forbidden.isdisjoint(protocol.__all__)


@pytest.mark.parametrize("contract_type", CONTRACT_TYPES)
def test_contract_types_are_frozen_slotted_and_keyword_only(contract_type) -> None:
    parameters = contract_type.__dataclass_params__
    assert parameters.frozen is True
    assert "__slots__" in contract_type.__dict__
    assert all(item.kw_only for item in dataclasses.fields(contract_type))


def test_schema_modes_are_a_stable_subset_of_runtime_single_agent_modes() -> None:
    schema_modes = {mode.value for mode in SingleAgentMode}

    assert schema_modes == {
        "agent.work.normal",
        "agent.work.plan",
        "agent.code.normal",
        "agent.code.plan",
    }
    assert schema_modes <= SINGLE_AGENT_CANONICAL_MODES


@pytest.mark.parametrize(
    "mode",
    ["team", "team.work.normal", "workflow", "auto_harness", "code.normal"],
)
def test_run_input_rejects_non_single_agent_or_legacy_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="unsupported single-Agent mode"):
        OneShotRunInput(input="hello", mode=mode)


def test_agent_and_workspace_round_trip_without_hidden_runtime_fields() -> None:
    run = OneShotRunInput(
        request_id="request-1",
        input="  hello  ",
        agent=_agent(),
        mode=SingleAgentMode.CODE_PLAN.value,
        workspace=WorkspaceSpec(
            cwd="D:/work",
            project_dir="D:/project",
            trusted_dirs=("D:/shared",),
        ),
        timeout_seconds=30,
    )

    encoded = run.to_dict()
    decoded = OneShotRunInput.from_dict(encoded)

    assert decoded == run
    assert decoded.input == "  hello  "
    assert decoded.agent is not None
    assert decoded.agent.instructions == "  keep instruction whitespace  "
    assert json.loads(json.dumps(encoded, ensure_ascii=False)) == encoded
    assert not ({"team", "workflow", "subagents", "transport"} & set(encoded))


def test_agent_capabilities_match_existing_definition_semantics() -> None:
    configured = AgentSpec.from_dict(
        {"name": "configured", "instructions": "Use configured capabilities"}
    )

    assert configured.to_dict()["tools"] == ["*"]
    assert configured.to_dict()["skills"] == []
    with pytest.raises(ValueError, match="tools must not be empty"):
        AgentSpec.from_dict(
            {"name": "empty_tools", "instructions": "invalid", "tools": []}
        )
    with pytest.raises(ValueError, match="must be the only"):
        AgentSpec(
            name="mixed_tools",
            instructions="invalid",
            tools=("*", "read_file"),
        )


def test_resume_identity_is_runtime_owned_and_agent_declaration_is_optional() -> None:
    run = OneShotRunInput(
        input="continue",
        session_id="session-1",
        workspace=WorkspaceSpec(cwd="D:/work"),
    )

    assert run.agent is None
    assert run.mode is None
    assert run.workspace is not None
    assert run.to_dict()["session_id"] == "session-1"

    decoded = OneShotRunInput.from_dict(
        {
            "schema_version": "0.1",
            "type": "run",
            "session_id": "session-1",
            "input": "continue",
        }
    )
    assert decoded.mode is None
    assert decoded.workspace is None


def test_new_session_has_a_fixed_single_agent_default_mode() -> None:
    run = OneShotRunInput(input="hello")

    assert run.mode == SingleAgentMode.CODE_NORMAL.value
    assert run.to_dict()["mode"] == "agent.code.normal"


@pytest.mark.parametrize(
    "field,value,error_type",
    [
        ("input", "   ", ValueError),
        ("timeout_seconds", 0, ValueError),
        ("timeout_seconds", True, TypeError),
        ("workspace", {}, TypeError),
        ("agent", {}, TypeError),
    ],
)
def test_run_input_rejects_ambiguous_values(
    field: str,
    value: object,
    error_type: type[Exception],
) -> None:
    arguments = {"input": "hello", field: value}
    with pytest.raises(error_type):
        OneShotRunInput(**arguments)


def test_decoders_reject_unknown_fields_and_wrong_record_types() -> None:
    encoded = OneShotRunInput(input="hello").to_dict()
    with pytest.raises(ValueError, match="unknown fields"):
        OneShotRunInput.from_dict({**encoded, "method": "initialize"})
    with pytest.raises(ValueError, match="must be 'run'"):
        OneShotRunInput.from_dict({**encoded, "type": "request"})


def test_runtime_event_adapter_preserves_none_payload_and_runtime_identity() -> None:
    runtime_event = RuntimeEvent(
        request_id="request-1",
        channel_id="process_cli",
        session_id="session-1",
        payload=None,
        metadata={"internal": "must not leak"},
        is_complete=True,
        ok=False,
    )

    event = OneShotEvent.from_runtime_event(runtime_event, sequence=0)

    assert event.payload is None
    assert event.event_type == "runtime.event"
    assert event.request_id == runtime_event.request_id
    assert event.session_id == runtime_event.session_id
    assert "metadata" not in event.to_dict()
    assert "is_complete" not in event.to_dict()
    assert "ok" not in event.to_dict()
    assert event.to_dict()["type"] == "event"
    assert OneShotEvent.from_dict(event.to_dict()) == event
    assert runtime_event.payload is None


def test_event_type_has_one_authoritative_value() -> None:
    with pytest.raises(ValueError, match="must match"):
        OneShotEvent(
            sequence=0,
            request_id="request-1",
            event_type="chat.final",
            payload={"event_type": "chat.delta"},
        )


def test_json_payloads_are_copied_frozen_and_round_trip() -> None:
    source = {"nested": {"items": [1, 2]}}
    event = OneShotEvent(
        sequence=1,
        request_id="request-1",
        event_type="chat.delta",
        payload=source,
    )
    source["nested"]["items"].append(3)

    assert event.to_dict()["payload"] == {"nested": {"items": [1, 2]}}
    with pytest.raises(TypeError):
        event.payload["new"] = True  # type: ignore[index]


def test_result_is_the_unique_terminal_record_shape() -> None:
    result = OneShotRunResult(
        sequence=2,
        request_id="request-1",
        session_id="session-1",
        status=RunStatus.COMPLETED.value,
        exit_code=0,
        output="  done\n",
        usage={"input_tokens": 10, "output_tokens": 2},
    )

    encoded = result.to_dict()

    assert encoded["type"] == "result"
    assert encoded["sequence"] == 2
    assert encoded["status"] == "completed"
    assert encoded["output"] == "  done\n"
    assert OneShotRunResult.from_dict(encoded) == result


def test_failed_result_requires_structured_error_and_nonzero_exit() -> None:
    error = RuntimeErrorInfo(
        code="runtime_unavailable",
        message="runtime unavailable",
        retryable=True,
        details={"attempt": 1},
    )
    result = OneShotRunResult(
        sequence=1,
        request_id="request-1",
        status=RunStatus.FAILED.value,
        exit_code=1,
        error=error,
    )

    assert OneShotRunResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="must contain an error"):
        OneShotRunResult(
            sequence=0,
            request_id="request-1",
            status=RunStatus.FAILED.value,
            exit_code=1,
        )
    with pytest.raises(ValueError, match="exit_code 0"):
        OneShotRunResult(
            sequence=0,
            request_id="request-1",
            session_id="session-1",
            status=RunStatus.COMPLETED.value,
            exit_code=1,
        )
    with pytest.raises(ValueError, match="session_id"):
        OneShotRunResult(
            sequence=0,
            request_id="request-1",
            status=RunStatus.COMPLETED.value,
            exit_code=0,
        )


def test_protocol_cold_import_does_not_load_runtime_core_or_transports() -> None:
    script = """
import sys
import jiuwenswarm.channels.process_cli.protocol

unexpected = sorted(
    name for name in sys.modules
    if name == 'jiuwenswarm.runtime.service'
    or name.startswith('jiuwenswarm.server.agent_ws_server')
    or name.startswith('jiuwenswarm.gateway')
    or name == 'websockets'
)
print('UNEXPECTED=' + repr(unexpected))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "UNEXPECTED=[]" in result.stdout


def test_protocol_modules_have_no_server_or_transport_imports() -> None:
    protocol_dir = (
        PROJECT_ROOT / "jiuwenswarm" / "channels" / "process_cli" / "protocol"
    )
    forbidden_prefixes = (
        "jiuwenswarm.gateway",
        "jiuwenswarm.server",
        "websockets",
    )
    violations: list[str] = []

    for source in protocol_dir.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module in imported:
                if module.startswith(forbidden_prefixes):
                    violations.append(f"{source.name}:{node.lineno}: {module}")

    assert violations == []
