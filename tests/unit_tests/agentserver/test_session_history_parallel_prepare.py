# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""History I/O overlaps assembly and drains before its session lock is released."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def router(monkeypatch, child):
    value = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    value._is_session_scoped_adapter = False
    value._session_adapter_locks = {}
    value._session_adapters = {}
    value._session_instance_config = {}
    value._session_instance_mode = "agent"
    value._session_instance_sub_mode = None
    monkeypatch.setattr(value, "_new_session_scoped_adapter", lambda _sid: child)
    monkeypatch.setattr(value, "_load_skill_retrieval_session_profile", lambda _sid: None)
    monkeypatch.setattr(value, "_session_instance_extra_create_kwargs", lambda: {})
    monkeypatch.setattr(value, "_touch_session_adapter", lambda _sid: None)
    monkeypatch.setattr(value, "_reload_session_adapter_if_stale", AsyncMock())
    return value


def child_adapter():
    return SimpleNamespace(restore_skill_retrieval_session=Mock(),
        configure_initial_execution=Mock(),
        persist_skill_retrieval_session_profile=Mock(), create_instance=AsyncMock(),
        start_interaction=AsyncMock(), _instance=object())


@pytest.mark.asyncio
async def test_history_read_overlaps_creation_and_restores_after_config_join(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    child = child_adapter()
    parent = router(monkeypatch, child)
    snapshot = object()
    stages = []

    def read(_sid, **_kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return snapshot

    async def create(*_args, **_kwargs):
        assert await asyncio.to_thread(entered.wait, 5)
        assert parent._session_adapter_locks["session-parallel"].locked()
        assert not finished.is_set()
        stages.append("create")
        release.set()

    async def start(**_kwargs):
        stages.append("start")

    async def reload(*_args, **_kwargs):
        stages.append("reload")

    async def restore(**kwargs):
        assert finished.is_set() and stages == ["create", "start", "reload"]
        assert kwargs["history_snapshot"] is snapshot
        assert kwargs["history_before_request_id"] == "one-input"
        assert parent._session_adapter_locks["session-parallel"].locked()
        stages.append("context")

    child.create_instance = create
    child.start_interaction = start
    monkeypatch.setattr(parent, "_reload_session_adapter_if_stale", reload)
    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    assert await parent._get_or_create_session_adapter("session-parallel",
        history_before_request_id="one-input") is child
    assert stages == ["create", "start", "reload", "context"]
    assert not parent._session_adapter_locks["session-parallel"].locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "assembly"])
async def test_failed_or_cancelled_assembly_drains_read_inside_session_lock(monkeypatch, failure):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    assembly_entered = asyncio.Event()
    child = child_adapter()
    parent = router(monkeypatch, child)

    def read(_sid, **_kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return None

    async def create(*_args, **_kwargs):
        assert await asyncio.to_thread(entered.wait, 5)
        assembly_entered.set()
        if failure == "assembly":
            raise RuntimeError("required rail failed")
        await asyncio.Event().wait()

    child.create_instance = create
    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    restore = AsyncMock()
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    task = asyncio.create_task(parent._get_or_create_session_adapter("session-drain",
        history_before_request_id="one-input"))
    await asyncio.wait_for(assembly_entered.wait(), 5)
    if failure == "cancel":
        task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done() and parent._session_adapter_locks["session-drain"].locked()
    if failure == "cancel":
        task.cancel()  # A repeated cancellation cannot bypass the drain.
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await task
    assert finished.is_set() and not parent._session_adapter_locks["session-drain"].locked()
    child.start_interaction.assert_not_awaited()
    restore.assert_not_awaited()
    assert parent._session_adapters == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation", ["none", "error", "no-cutoff"])
async def test_unavailable_pre_read_preserves_original_warmup_call(monkeypatch, preparation):
    child = child_adapter()
    parent = router(monkeypatch, child)

    def read(_sid, **_kwargs):
        if preparation == "no-cutoff":
            pytest.fail("create/switch preparation must keep the original transaction")
        if preparation == "error":
            raise OSError("disk unavailable")
        return None

    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    restore = AsyncMock()
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    boundary = None if preparation == "no-cutoff" else "one-input"
    assert await parent._get_or_create_session_adapter("session-fallback",
        history_before_request_id=boundary) is child
    assert restore.await_args.kwargs == {"deep_agent": child._instance,
        "session_id": "session-fallback", "history_before_request_id": boundary}
