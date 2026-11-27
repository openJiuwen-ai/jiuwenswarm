# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Subagent data models for Skills subagent spawning capability."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubagentRoleDefinition(BaseModel):
    """Role definition declared in Skill frontmatter.

    Defines a subagent role with its system prompt and optional skill binding.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    system_prompt: str
    skill_path: str | None = None
    allowed_tools: tuple[str, ...] | None = None


class SubagentConfig(BaseModel):
    """Skill-declared subagent configuration (frontmatter).

    Example in SKILL.md:
    ```yaml
    subagent:
      enabled: true
      parallel_max: 3
      default_role: "Alice"
      roles:
        Alice:
          name: "Researcher Alice"
          system_prompt: "You are a professional researcher..."
          skill_path: "pptx-craft/outline-research"
    ```
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    roles: dict[str, SubagentRoleDefinition] = Field(default_factory=dict)
    parallel_max: int = 3
    default_role: str = "MainAgent"


class SubagentTaskSpec(BaseModel):
    """Specification for a single subagent task.

    Used when calling spawn_subagent() tool.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(
        default_factory=lambda: f"subagent_{uuid.uuid4().hex[:8]}"
    )
    role_id: str = "MainAgent"
    objective: str
    prompt: str = ""


class SubagentResult(BaseModel):
    """Result from subagent execution."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    task_id: str
    role_id: str
    result: str | None = None
    error: str | None = None
    output_files: list[str] = Field(default_factory=list)
    usage: dict[str, int] | None = None


class ForkAgentTaskSpec(BaseModel):
    """Fork Agent task specification.

    Unlike SubagentTaskSpec, ForkAgent inherits messages prefix from parent Agent,
    enabling KVCache reuse and consistent context understanding.

    Key differences from SubagentTaskSpec:
    - No skill_path (fork inherits parent's context, not a separate skill)
    - No system_prompt override (uses parent's messages prefix)
    - Designed for parallel/serial execution determined by model
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(
        default_factory=lambda: f"fork_agent_{uuid.uuid4().hex[:8]}"
    )
    objective: str
    prompt: str = ""
    role_id: str = "ForkedWorker"  # 内部使用，不再从工具入参传入


class ForkAgentResult(BaseModel):
    """Result from ForkAgent execution."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    task_id: str
    role_id: str
    result: str | None = None
    error: str | None = None
    output_files: list[str] = Field(default_factory=list)
    usage: dict[str, int] | None = None