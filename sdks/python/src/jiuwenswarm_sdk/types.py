# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Optional typing helpers. Runtime remains the definition/policy validator."""

from typing import Literal, NotRequired, TypedDict

Mode = Literal[
    "agent.code.normal", "agent.code.plan", "agent.work.normal", "agent.work.plan"
]
QueryOperation = Literal[
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
    cwd: str
    project_dir: str
    trusted_dirs: list[str]


class AgentDefinition(TypedDict):
    name: str
    instructions: str
    description: NotRequired[str]
    model: NotRequired[str]
    tools: NotRequired[list[str]]
    skills: NotRequired[list[str]]
    max_iterations: NotRequired[int]


class RunInput(TypedDict):
    input: str
    request_id: NotRequired[str]
    session_id: NotRequired[str]
    mode: NotRequired[Mode]
    agent: NotRequired[AgentDefinition]
    workspace: NotRequired[Workspace]
    timeout_seconds: NotRequired[float]
