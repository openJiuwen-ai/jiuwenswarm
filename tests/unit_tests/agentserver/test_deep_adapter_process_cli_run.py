# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for Process CLI options on a reused root adapter."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway_protocol.e2a.agent_models import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _session_child(failure_stage: str | None = None) -> SimpleNamespace:
    async def stream(request, inputs):
        if failure_stage == "stream":
            raise ValueError("first run failed")
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"session_id": request.session_id},
            is_complete=True,
        )

    return SimpleNamespace(
        configure_process_cli_run=AsyncMock(
            side_effect=ValueError("first run failed")
            if failure_stage == "configure"
            else None
        ),
        process_message_stream_impl=stream,
        _unregister_session_agent_task=MagicMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", [None, "configure", "stream"])
async def test_run_options_are_not_reused_by_later_sessions(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str | None,
) -> None:
    parent = JiuWenSwarmDeepAdapter()
    parent._channel_id = "process_cli"
    children = [_session_child(failure_stage), _session_child(), _session_child()]
    monkeypatch.setattr(
        parent, "_get_session_adapter_for_request", AsyncMock(side_effect=children)
    )
    evict = AsyncMock()
    monkeypatch.setattr(parent, "_evict_idle_session_adapters", evict)

    async def run(session_id):
        request = AgentRequest(
            request_id=f"request-{session_id}",
            channel_id="process_cli",
            session_id=session_id,
        )
        return [
            chunk
            async for chunk in parent._process_message_stream_impl(
                request, {"query": "hello"}
            )
        ]

    old_tool = object()
    await parent.configure_process_cli_run(max_turns=5, host_tools=(old_tool,))
    if failure_stage is None:
        assert len(await run("first")) == 1
    else:
        with pytest.raises(ValueError, match="first run failed"):
            await run("first")
    children[0].configure_process_cli_run.assert_awaited_once_with(
        max_turns=5, host_tools=(old_tool,)
    )

    # A run with no ready callback must receive neither the previous limit
    # nor a host tool bound to the previous controller, even after a failure.
    assert len(await run("second")) == 1
    children[1].configure_process_cli_run.assert_not_called()

    new_tool = object()
    await parent.configure_process_cli_run(max_turns=7, host_tools=(new_tool,))
    assert len(await run("third")) == 1
    children[2].configure_process_cli_run.assert_awaited_once_with(
        max_turns=7, host_tools=(new_tool,)
    )
    for child, session_id in zip(children, ("first", "second", "third"), strict=True):
        child._unregister_session_agent_task.assert_called_once_with(session_id)
    assert evict.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["process_cli", "web", "tui"])
async def test_only_process_cli_flushes_stopped_owned_interaction_session(channel):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._channel_id = channel
    session = SimpleNamespace(post_run=AsyncMock())
    instance = SimpleNamespace(interaction_started=True, loop_session=session)

    async def stop():
        instance.interaction_started = False

    instance.stop = AsyncMock(side_effect=stop)
    adapter._instance = instance
    await adapter.stop_interaction()
    await adapter.stop_interaction()
    assert session.post_run.await_count == (1 if channel == "process_cli" else 0)


@pytest.mark.asyncio
async def test_process_cli_checkpoint_failure_propagates_from_stop():
    adapter = JiuWenSwarmDeepAdapter()
    adapter._channel_id = "process_cli"
    adapter._instance = SimpleNamespace(
        interaction_started=True,
        stop=AsyncMock(),
        loop_session=SimpleNamespace(
            post_run=AsyncMock(side_effect=OSError("checkpoint unavailable"))
        ),
    )
    with pytest.raises(OSError, match="checkpoint unavailable"):
        await adapter.stop_interaction()
