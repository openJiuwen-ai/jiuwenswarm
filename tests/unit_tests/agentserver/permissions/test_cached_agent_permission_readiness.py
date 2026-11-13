# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import jiuwenswarm.agents.harness.common.tools.multi_session_toolkits as multi_session_toolkits_module
from jiuwenswarm.agents.harness.common.tools.multi_session_toolkits import (
    MultiSessionToolkit,
    SessionTask,
    Status,
)


class _CachedAgent:
    def __init__(self) -> None:
        self.executions = 0
        self.executed = asyncio.Event()

    async def process_message_stream(self, _request):
        self.executions += 1
        self.executed.set()
        if False:
            yield None

    def get_instance(self):
        return self

    async def ensure_instance(self):
        return self


class _ReadinessManager:
    def __init__(self, agent: _CachedAgent) -> None:
        self.agent = agent
        self.lookups = 0

    async def wait_for_permissions_ready(self) -> None:
        raise AssertionError("ordinary cached execution must not wait for global reload")

    def get_agent_nowait(self, _channel_id: str):
        self.lookups += 1
        return self.agent


def _multi_session_toolkit() -> MultiSessionToolkit:
    toolkit = MultiSessionToolkit(
        session_id="parent-session",
        channel_id="web",
        request_id="request-1",
        sub_agent_config=SimpleNamespace(),
    )
    toolkit.sessions.append(
        SessionTask(
            session_id="child-session",
            description="finished task",
            status=Status.COMPLETED,
            result="done",
        )
    )
    return toolkit


@pytest.mark.asyncio
async def test_multi_session_ordinary_cached_execution_does_not_wait_for_global_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _CachedAgent()
    manager = _ReadinessManager(agent)
    toolkit = _multi_session_toolkit()
    monkeypatch.setattr(
        multi_session_toolkits_module,
        "get_current_agent_manager",
        lambda: manager,
    )

    async def run_agent_streaming(_agent, *, inputs):
        _ = inputs
        agent.executions += 1
        yield SimpleNamespace(type="answer", payload={"output": "summary"})

    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(Runner, "run_agent_streaming", run_agent_streaming)
    await toolkit.notify("child-session", Status.COMPLETED, result="done")
    assert manager.lookups == 1
    assert agent.executions == 1


@pytest.mark.asyncio
async def test_multi_session_without_cached_agent_keeps_raw_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _CachedAgent()
    manager = _ReadinessManager(agent)
    toolkit = _multi_session_toolkit()
    monkeypatch.setattr(
        multi_session_toolkits_module,
        "get_current_agent_manager",
        lambda: manager,
    )

    manager.agent = None
    await toolkit.notify("child-session", Status.COMPLETED, result="done")
    assert manager.lookups == 1
    assert agent.executions == 0
