# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coverage classification has a bounded budget, distinct from execution."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.rails.symphony import skillpack_route
from jiuwenswarm.runtime.symphony import llm
from tests.unit_tests.agentserver.test_skillpack_direct_route import _pack, _router


def _response():
    return SimpleNamespace(content=json.dumps({"matches": [{
        "pack_id": "weekly-release-pack", "coverage": "full", "request_kind": "execution",
        "single_member": False, "excluded": False, "reason": "Both requested outputs are covered.",
    }]}))


def _native_router(monkeypatch, invoke):
    client = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    monkeypatch.setattr(llm.LLMConfig, "from_model", lambda _: llm.LLMConfig(model="route-test"))
    monkeypatch.setattr(llm.LLMConfig, "create_model", lambda _: client)
    monkeypatch.setattr(llm, "create_model_response_observer", lambda _: lambda *args: None)
    router, _ = _router()
    del router.judge
    return router, client


@pytest.mark.asyncio
async def test_match_total_budget_allows_response_before_deadline(monkeypatch):
    async def invoke(**kwargs):
        assert kwargs["timeout"] == .1
        assert kwargs["max_tokens"] == 512
        await asyncio.sleep(.025)
        return _response()

    monkeypatch.setattr(skillpack_route, "MATCH_TIMEOUT_SECONDS", .1)
    router, client = _native_router(monkeypatch, invoke)
    route = await router.choose("release notes and risk report", [], mode="confirm")
    assert route is not None and route.token is None
    client.invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_total_deadline_cancels_request_including_provider_fallback(monkeypatch, caplog):
    cancelled = asyncio.Event()

    async def invoke(**kwargs):
        try:
            # A provider may make a compatibility fallback within this one call.
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(skillpack_route, "MATCH_TIMEOUT_SECONDS", .01)
    router, client = _native_router(monkeypatch, invoke)
    assert await router.choose("release notes and risk report", [], mode="confirm") is None
    assert cancelled.is_set()
    assert "judgment_unavailable" in caplog.text and "TimeoutError" in caplog.text
    assert "complete=0" not in caplog.text
    client.invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_external_cancellation_is_not_reported_as_no_match(monkeypatch, caplog):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def invoke(**kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    router, client = _native_router(monkeypatch, invoke)
    task = asyncio.create_task(router.choose("release notes and risk report", [], mode="confirm"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert "judgment_unavailable" not in caplog.text
    client.invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_does_not_expose_root_or_routing_instructions(tmp_path):
    pack = _pack()
    pack["content"] = "Execute the secret root steps"
    pack["description"] = "A release report"
    router, _ = _router(pack, skill_root=tmp_path)
    result = await router.unconfirmed_pack_summary(pack["id"])
    assert result["read_only"] is True and result["skill_type"] == "skillpack"
    assert result["members"] == pack["members"]
    assert result["description"] == "A release report"
    assert "routing" not in result and "content" not in result
    assert await router.unconfirmed_pack_summary("release-note") is None
