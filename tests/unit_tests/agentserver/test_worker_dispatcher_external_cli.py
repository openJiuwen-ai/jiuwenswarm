# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for refreshing managed CLI SDK paths on the request path."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.worker import dispatcher as worker_dispatcher


def _request() -> AgentRequest:
    return AgentRequest(
        request_id=uuid.uuid4().hex,
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
        is_stream=False,
        agent_ref=None,
        user_id="user-1",
    )


def _bare_dispatcher(runtime: object, holder_generation: int = 1) -> worker_dispatcher.WorkerDispatcher:
    """Build a dispatcher with only the attributes ``_run_request`` touches."""
    instance = worker_dispatcher.WorkerDispatcher.__new__(worker_dispatcher.WorkerDispatcher)
    instance._bridge = SimpleNamespace(
        events=SimpleNamespace(
            register=lambda request, dispatch_id: None,
            complete=lambda dispatch_id, **kwargs: None,
            finish=lambda dispatch_id: None,
        ),
        observe_execution_entry=lambda *args, **kwargs: None,
        holder=SimpleNamespace(generation=holder_generation, conn=None),
    )
    instance._state_lock = asyncio.Lock()
    instance._lock = asyncio.Lock()
    instance._draining = False
    instance._runtime = runtime
    instance._ready = True
    instance._failure = None
    instance._queued = []
    instance._startup_priorities = {}
    return instance


@pytest.mark.asyncio
async def test_run_request_refreshes_external_cli_paths_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn must see SDKs installed after the resident worker started."""
    activated: list[bool] = []
    dispatched: list[AgentRequest] = []

    class _Runtime:
        async def dispatch_parsed_request(self, bridge, request, lock):
            dispatched.append(request)

    monkeypatch.setattr(
        worker_dispatcher,
        "activate_external_cli_runtime_paths",
        lambda: activated.append(True),
    )
    instance = _bare_dispatcher(_Runtime())

    await instance._run_request(_request(), 1)

    assert activated == [True]
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_run_request_serves_despite_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optional SDK problem must not fail the request."""
    dispatched: list[AgentRequest] = []

    class _Runtime:
        async def dispatch_parsed_request(self, bridge, request, lock):
            dispatched.append(request)

    def _raise_activation() -> None:
        raise RuntimeError("broken managed runtime layout")

    monkeypatch.setattr(
        worker_dispatcher, "activate_external_cli_runtime_paths", _raise_activation
    )
    instance = _bare_dispatcher(_Runtime())

    await instance._run_request(_request(), 1)

    assert len(dispatched) == 1
