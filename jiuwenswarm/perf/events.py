# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmPerfEvent:
    llm_call_id: str
    duration_ms: float
    model: str
    iteration: int
    input_tokens: int
    output_tokens: int
    status: str
    agent_id: str = ""
    task_id: str | None = None
    stream_source_id: str | None = None
    error_message: str | None = None
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class ToolPerfEvent:
    tool_call_id: str
    name: str
    duration_ms: float
    status: str
    agent_id: str = ""
    task_id: str | None = None
    iteration: int = 0
    error_message: str | None = None


@dataclass(frozen=True)
class TaskPerfEvent:
    task_id: str
    task_content: str
    source: str
    started_at: float
    ended_at: float
    duration_ms: float
    status: str
