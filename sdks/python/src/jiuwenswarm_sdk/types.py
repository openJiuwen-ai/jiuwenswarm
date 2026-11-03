# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Optional typing helpers. Runtime remains the definition/policy validator."""

from typing import Literal, NotRequired, TypedDict

Mode = Literal[
    "agent.code.normal", "agent.code.plan", "agent.work.normal", "agent.work.plan"
]
QueryOperation = Literal[
    "protocol.capabilities",
    "protocol.schema",
    "session.get",
    "session.list",
    "model.list",
    "model.resolve",
    "mode.list",
    "mode.resolve",
    "permission.get",
    "mcp.validate",
]


class Workspace(TypedDict, total=False):
    cwd: str | None
    project_dir: str | None
    trusted_dirs: list[str]


class AgentDefinition(TypedDict):
    name: str
    instructions: str
    description: NotRequired[str | None]
    model: NotRequired[str | None]
    tools: NotRequired[list[str]]
    skills: NotRequired[list[str]]
    max_iterations: NotRequired[int | None]


class RunInput(TypedDict):
    input: str
    request_id: NotRequired[str | None]
    session_id: NotRequired[str | None]
    mode: NotRequired[Mode | None]
    model: NotRequired[str | None]
    skills: NotRequired[list[str] | None]
    mcp: NotRequired[list[str] | None]
    permissions: NotRequired["RunPermissions | None"]
    output_schema: NotRequired[dict[str, object] | None]
    max_turns: NotRequired[int | None]
    max_budget_usd: NotRequired[float | None]
    host_tools: NotRequired[list["HostTool"]]
    agent: NotRequired[AgentDefinition | None]
    workspace: NotRequired[Workspace | None]
    timeout_seconds: NotRequired[float | None]


class RunPermissions(TypedDict, total=False):
    tools: dict[str, Literal["allow", "ask", "deny"]]


class HostTool(TypedDict):
    name: str
    description: str
    input_schema: dict[str, object]


class TextPayload(TypedDict):
    text: str


class ToolObservation(TypedDict):
    call_id: str
    name: str
    arguments: dict[str, object] | None
    status: Literal["started", "completed", "failed"]
    result: object


class ToolPayload(TypedDict):
    tool: ToolObservation


class InteractionOption(TypedDict):
    value: str
    label: str


class InteractionQuestion(TypedDict):
    question_id: str
    question: str
    options: list[InteractionOption]
    card_id: str | None
    allow_custom_input: bool


class InteractionPayload(TypedDict):
    interaction_id: str
    kind: Literal["permission", "question", "confirmation"]
    questions: list[InteractionQuestion]
    interaction: dict[str, object]


class HostToolPayload(TypedDict):
    call_id: str
    name: str
    arguments: dict[str, object]


class ProtocolCapabilities(TypedDict):
    schema_version: str
    supported_schema_versions: list[str]
    protocol_revision: int
    features: dict[str, bool]
    run_fields: list[str]
    query_operations: list[str]
    control_types: list[str]
    stable_event_types: list[str]
    input_encoding: str
    output_encoding: str
    output_format: str
    max_input_bytes: int
    compatibility: dict[str, str]
