# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.runtime.agent_definition import (
    RuntimeAgentDefinition,
    RuntimeAgentDefinitionError,
    RuntimeAgentDefinitionErrorCode,
    RuntimeAgentExecution,
    RuntimeAgentMode,
    prepare_agent_execution,
)


pytestmark = pytest.mark.unit
SOURCE = Path(__file__).parents[3] / "jiuwenswarm" / "runtime" / "agent_definition.py"


def _definition(**overrides: object) -> RuntimeAgentDefinition:
    values: dict[str, Any] = {
        "name": "local_agent",
        "instructions": "  Preserve instruction whitespace.  ",
        "description": " local root agent ",
        "model": " GLM-5.2 ",
        "tools": "*",
        "skills": ("review", "testing"),
        "max_iterations": 12,
    }
    values.update(overrides)
    return RuntimeAgentDefinition.from_mapping(values)


@pytest.mark.parametrize(
    "contract_type", [RuntimeAgentDefinition, RuntimeAgentExecution]
)
def test_runtime_agent_contracts_are_frozen_slotted_and_keyword_only(
    contract_type: type[Any],
) -> None:
    assert getattr(contract_type, "__dataclass_params__").frozen is True
    assert "__slots__" in contract_type.__dict__
    assert all(field.kw_only for field in dataclasses.fields(contract_type))


def test_definition_is_normalized_without_rewriting_instructions() -> None:
    definition = _definition()

    assert definition.name == "local_agent"
    assert definition.instructions == "  Preserve instruction whitespace.  "
    assert definition.description == "local root agent"
    assert definition.model == "GLM-5.2"
    assert definition.tools == "*"
    assert definition.skills == ("review", "testing")


def test_strict_mapping_round_trip_accepts_both_wildcard_forms() -> None:
    expected = _definition()
    decoded = RuntimeAgentDefinition.from_mapping(expected.to_dict())
    list_wildcard = RuntimeAgentDefinition.from_mapping(
        {
            "name": "local_agent",
            "instructions": "  Preserve instruction whitespace.  ",
            "description": "local root agent",
            "model": "GLM-5.2",
            "tools": ["*"],
            "skills": ["review", "testing"],
            "max_iterations": 12,
        }
    )

    assert decoded == expected
    assert list_wildcard == expected


def test_mutable_mapping_inputs_are_copied_into_the_frozen_contract() -> None:
    skills = ["review"]
    definition = RuntimeAgentDefinition.from_mapping(
        {
            "name": "local_agent",
            "instructions": "run",
            "skills": skills,
        }
    )

    skills.append("changed-after-parse")

    assert definition.skills == ("review",)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"name": "agent_name"},
        {"instructions": "run"},
        {"name": "agent_name", "instructions": "run", "extra": True},
        {1: "not-a-string-key", "name": "agent_name", "instructions": "run"},
    ],
)
def test_definition_mapping_is_strict(value: object) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        RuntimeAgentDefinition.from_mapping(value)

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("name", "expected_message"),
    [
        ("ab", "name must match"),
        ("a" * 51, "name must match"),
        ("contains space", "name must match"),
        ("非法名称", "name must match"),
        ("   ", "name must not be empty"),
        (None, "name must be a string"),
    ],
)
def test_name_is_strict(name: object, expected_message: str) -> None:
    with pytest.raises(RuntimeAgentDefinitionError, match=expected_message) as caught:
        _definition(name=name)

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.field == "name"


@pytest.mark.parametrize("instructions", [None, 7, "", " \t\r\n "])
def test_instructions_are_required_non_blank_text(instructions: object) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        _definition(instructions=instructions)

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.field == "instructions"


@pytest.mark.parametrize("max_iterations", [True, False, 1.5, "10", 0, -1])
def test_max_iterations_is_a_strict_positive_integer(max_iterations: object) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        _definition(max_iterations=max_iterations)

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.field == "max_iterations"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", " "),
        ("description", 1),
        ("model", " "),
        ("model", False),
        ("skills", "review"),
        ("skills", ("review", "review")),
        ("skills", ("review", 1)),
    ],
)
def test_optional_fields_and_skills_are_strict(field: str, value: object) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        _definition(**{field: value})

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.field is not None


@pytest.mark.parametrize(
    "tools",
    ["read_file", ("read_file",), ["read_file", "write_file"], ["*", "read_file"]],
)
def test_explicit_tool_allowlist_has_stable_unsupported_error(tools: object) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        _definition(tools=tools)

    assert caught.value.code == "AGENT_DEFINITION_TOOL_ALLOWLIST_UNSUPPORTED"
    assert caught.value.field == "tools"
    assert caught.value.retryable is False


@pytest.mark.parametrize("tools", [None, 7, (), [], [""], [1]])
def test_malformed_or_empty_tool_policy_is_invalid(tools: object) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        _definition(tools=tools)

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.field is not None


def test_fingerprint_is_stable_sha256_of_normalized_content() -> None:
    first = _definition()
    equivalent = RuntimeAgentDefinition.from_mapping(first.to_dict())

    assert first.fingerprint == equivalent.fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.fingerprint)
    assert (
        first.fingerprint
        == "548e7047512876c206c24391b0e10904a5327b3d8558e87fc0d38b031d4b1dcd"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "other_agent"),
        ("instructions", "Different instructions"),
        ("description", "Different description"),
        ("model", "other-model"),
        ("skills", ("different",)),
        ("max_iterations", 13),
    ],
)
def test_fingerprint_changes_with_each_supported_semantic_field(
    field: str,
    value: object,
) -> None:
    assert _definition().fingerprint != _definition(**{field: value}).fingerprint


@pytest.mark.parametrize(
    "mode", [RuntimeAgentMode.CODE_NORMAL, RuntimeAgentMode.CODE_PLAN]
)
def test_code_modes_prepare_a_transport_neutral_execution(
    mode: RuntimeAgentMode,
) -> None:
    definition = _definition()
    execution = prepare_agent_execution(definition, mode=mode.value)

    assert execution.definition is definition
    assert execution.mode is mode
    assert execution.fingerprint == definition.fingerprint
    assert execution.to_dict() == {
        "agent": definition.to_dict(),
        "agent_fingerprint": definition.fingerprint,
        "mode": mode.value,
    }


@pytest.mark.parametrize(
    "mode", [RuntimeAgentMode.WORK_NORMAL, RuntimeAgentMode.WORK_PLAN]
)
def test_work_modes_return_stable_unsupported_error(mode: RuntimeAgentMode) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        prepare_agent_execution(_definition(), mode=mode)

    assert caught.value.code == "AGENT_DEFINITION_MODE_UNSUPPORTED"
    assert caught.value.field == "mode"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "mode",
    [
        "agent",
        "code.normal",
        "team.work.normal",
        "workflow",
        "auto_harness",
        "agent.code.normal ",
        "",
        None,
    ],
)
def test_noncanonical_or_non_single_agent_modes_are_invalid(mode: Any) -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        prepare_agent_execution(_definition(), mode=mode)

    assert caught.value.code == "AGENT_DEFINITION_MODE_INVALID"
    assert caught.value.field == "mode"


def test_prepare_accepts_mapping_and_validates_before_mode_capability() -> None:
    with pytest.raises(RuntimeAgentDefinitionError) as caught:
        prepare_agent_execution(
            {"name": "x", "instructions": "run"},
            mode=RuntimeAgentMode.WORK_NORMAL,
        )

    assert caught.value.code == "AGENT_DEFINITION_INVALID"
    assert caught.value.field == "name"


def test_runtime_contract_has_no_execution_or_transport_dependencies() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    forbidden_imports = (
        "jiuwenswarm.channels.process_cli",
        "jiuwenswarm.gateway",
        "jiuwenswarm.server",
        "websocket",
        "websockets",
    )
    forbidden_symbols = {
        "AgentAdapter",
        "AgentManager",
        "AgentRuntime",
        "InProcessRuntimeClient",
    }
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            modules = []
        violations.extend(
            f"{getattr(node, 'lineno', 0)}: import {module}"
            for module in modules
            if module.startswith(forbidden_imports)
        )
        if isinstance(node, ast.Name) and node.id in forbidden_symbols:
            violations.append(f"{node.lineno}: name {node.id}")

    assert violations == []


def test_public_surface_is_explicit_and_runtime_owned() -> None:
    import jiuwenswarm.runtime.agent_definition as contract

    assert {item.value for item in RuntimeAgentDefinitionErrorCode} == {
        "AGENT_DEFINITION_INVALID",
        "AGENT_DEFINITION_REQUEST_INVALID",
        "AGENT_DEFINITION_MODE_INVALID",
        "AGENT_DEFINITION_MODE_UNSUPPORTED",
        "AGENT_DEFINITION_MODEL_NOT_FOUND",
        "AGENT_DEFINITION_TOOL_ALLOWLIST_UNSUPPORTED",
        "AGENT_DEFINITION_SESSION_CONFLICT",
    }
    assert set(contract.__all__) == {
        "RuntimeAgentDefinition",
        "RuntimeAgentDefinitionError",
        "RuntimeAgentDefinitionErrorCode",
        "RuntimeAgentExecution",
        "RuntimeAgentMode",
        "prepare_agent_execution",
    }
