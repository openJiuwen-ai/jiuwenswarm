# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team mode installs the same permission context bindings as a single agent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    """Build a bare adapter whose team branch can run without a real agent."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = object()
    adapter._is_session_scoped_adapter = True
    adapter._config_cache = {}
    adapter._project_dir = "/tmp/project"
    adapter._workspace_dir = "/tmp/workspace"
    adapter._runtime_prompt_rail = None
    adapter._has_valid_model_config = lambda _model: True
    adapter._resolve_model_for_request = lambda _request: None
    adapter._apply_model_to_react_agent = lambda _model: None
    adapter._native_image_input_enabled = lambda _config, _model: False
    adapter._build_image_tool_fallback_notice = lambda *_args, **_kwargs: None
    adapter._prepare_multimodal_image_inputs = staticmethod(lambda _request, inputs: inputs)
    adapter._prepare_react_image_tool_prompt = staticmethod(
        lambda _request, inputs, **_kwargs: inputs
    )
    adapter._resolve_runtime_language = lambda: "zh"
    adapter._resolve_prompt_channel = lambda _session_id: "acp"
    adapter._resolve_model_name = lambda: "test-model"
    adapter._write_runtime_state = lambda **_kwargs: None
    return adapter


def _team_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-team-acp",
        channel_id="acp",
        session_id="sess-team-acp",
        params={"query": "分析这个仓库", "mode": "team"},
    )


async def _drain(adapter: JiuWenSwarmDeepAdapter, request: AgentRequest) -> None:
    async for _chunk in adapter.process_message_stream_impl(request, {"query": "分析这个仓库"}):
        pass


@pytest.mark.anyio
async def test_team_branch_binds_permission_channel_id(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, Any] = {}

    async def _fake_stream(_request, _inputs, _agent):
        seen["during_stream"] = TOOL_PERMISSION_CHANNEL_ID.get()
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(team_helpers, "process_team_message_stream", _fake_stream)

    await _drain(_make_adapter(), _team_request())

    assert seen["during_stream"] == "acp"


@pytest.mark.anyio
async def test_team_branch_resets_permission_channel_id_after_request(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_stream(_request, _inputs, _agent):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(team_helpers, "process_team_message_stream", _fake_stream)
    before = TOOL_PERMISSION_CHANNEL_ID.get()

    await _drain(_make_adapter(), _team_request())

    assert TOOL_PERMISSION_CHANNEL_ID.get() == before


@pytest.mark.anyio
async def test_team_background_task_keeps_binding_after_request_ends(
    monkeypatch: pytest.MonkeyPatch,
):
    """The team stream task outlives the request; its context snapshot must hold.

    ``_consume_stream_with_query`` runs in a task spawned during the request,
    so the reset that fires when the request ends must not strip the binding
    the still-running team round depends on.
    """
    observed: list[str] = []
    started = asyncio.Event()

    async def _background() -> None:
        await started.wait()
        observed.append(TOOL_PERMISSION_CHANNEL_ID.get())

    task: list[asyncio.Task] = []

    async def _fake_stream(_request, _inputs, _agent):
        task.append(asyncio.create_task(_background()))
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(team_helpers, "process_team_message_stream", _fake_stream)

    await _drain(_make_adapter(), _team_request())
    # Request is over and the reset has run; only now let the task read.
    started.set()
    await task[0]

    assert observed == ["acp"]
    assert TOOL_PERMISSION_CHANNEL_ID.get() != "acp"
