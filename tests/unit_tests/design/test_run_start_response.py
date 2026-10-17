# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The run-start response acknowledges the new trigger before media completes."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter


@pytest.mark.parametrize("prior_status", [None, "completed", "failed"])
async def test_start_returns_new_running_snapshot_before_trigger_finishes(monkeypatch, prior_status):
    gate = asyncio.Event()
    graph = {"graph_id": "g_start", "metadata": {"team_design_run_id": "run_start"}}
    previous = {"run_id": "run_start", "status": prior_status} if prior_status else None
    snapshot = {
        "run_id": "run_start", "graph_id": "g_start", "status": "running",
        "node_states": {"n_image": {"version": 2}},
    }
    pushes = []

    async def start(graph, *, node_ids, cascade, on_update):
        on_update(snapshot)
        await gate.wait()
        snapshot["status"] = "completed"
        on_update(snapshot, "n_image")
        return snapshot

    async def push(**kwargs):
        pushes.append(deepcopy(kwargs["payload"]))

    coordinator = SimpleNamespace(is_active=lambda: False, start=start)
    monkeypatch.setattr(adapter, "_coordinator_for", lambda _: coordinator)
    monkeypatch.setattr(adapter, "_store", SimpleNamespace(get_run=lambda _: previous))
    monkeypatch.setattr(adapter, "_start_run", lambda _: (
        {"graph": graph, "graph_id": "g_start", "node_ids": ["n_image"]}, None, None,
    ))
    monkeypatch.setattr(adapter, "_push_designer_event", push)
    request = AgentRequest(request_id="start", channel_id="web", session_id="design_real",
                           req_method=ReqMethod.DESIGNER_RUN_START)
    try:
        response = await asyncio.wait_for(adapter.DesignerAdapter().handle(request), timeout=1)
        assert response.ok
        assert response.payload["run"] == snapshot
        assert response.payload["run"]["status"] == "running"
        assert not gate.is_set()
    finally:
        gate.set()
        await asyncio.gather(*list(adapter._RUN_TASKS))
    await asyncio.sleep(0)
    assert response.payload["run"]["status"] == "running"
    assert any(item.get("node_id") == "n_image" and item["run"]["status"] == "completed" for item in pushes)


async def test_start_reports_failure_before_initial_snapshot(monkeypatch):
    async def start(*args, **kwargs):
        raise ValueError("cannot prepare trigger")

    coordinator = SimpleNamespace(is_active=lambda: False, start=start)
    monkeypatch.setattr(adapter, "_coordinator_for", lambda _: coordinator)
    monkeypatch.setattr(adapter, "_start_run", lambda _: (
        {"graph": {"graph_id": "g_start"}, "graph_id": "g_start", "node_ids": None}, None, None,
    ))
    request = AgentRequest(request_id="start", channel_id="web", req_method=ReqMethod.DESIGNER_RUN_START)
    response = await asyncio.wait_for(adapter.DesignerAdapter().handle(request), timeout=1)
    await asyncio.gather(*list(adapter._RUN_TASKS))
    assert response.ok is False
    assert "cannot prepare trigger" in str(response.payload)
