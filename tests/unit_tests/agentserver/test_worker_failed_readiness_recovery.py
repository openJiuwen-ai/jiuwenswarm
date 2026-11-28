# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-68: a Worker-reported FAILED readiness must schedule automatic recovery.

A load-failed Worker stays resident on its IPC socket and never retries on its
own. Before the fix the supervisor marked the failure sticky and only a config
reload could exit FAILED, so a dependency drift kept the runtime down until a
full Front restart. The supervisor now routes Worker-reported FAILED frames
through the retryable-failure path, and the forwarding module for the pinned
agent-gateway SPI symbols degrades to ``None`` instead of hard-failing imports.
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@pytest.mark.asyncio
async def test_worker_failed_readiness_schedules_automatic_recovery():
    """A FAILED frame from a live resident Worker starts the recovery task."""
    from jiuwenswarm.server.supervisor.worker_registry import WorkerSlot

    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, max_restarts=3)
    try:
        supervisor._validated_conn = object()
        supervisor._registry.upsert(
            WorkerSlot(worker_id="primary", pid=1, socket_path="", ready=True)
        )
        supervisor._apply_readiness({
            "state": "FAILED",
            "reason": "cannot import name 'SKILL_CAPABILITIES' from 'agent_gateway.extensions.sdk.skill_source'",
        })
        assert readiness.state is ReadinessState.FAILED
        assert supervisor._fatal
        assert supervisor._retryable_failure
        assert supervisor._automatic_recovery_task is not None
        assert not supervisor._automatic_recovery_task.done()
        # The failure reason is preserved for gateway error frames.
        assert "SKILL_CAPABILITIES" in supervisor._fatal_reason
        assert not readiness.snapshot()["agent_ready"]
    finally:
        await supervisor.stop()
    assert supervisor._automatic_recovery_task is None


@pytest.mark.asyncio
async def test_worker_failed_readiness_recovers_after_dependency_is_fixed(
    monkeypatch, tmp_path
):
    """Automatic recovery replaces the failed resident without a config reload."""
    from jiuwenswarm.server.supervisor import runtime_supervisor as module

    monkeypatch.setattr(module, "_RECOVERY_BACKOFF_INITIAL", 0.1)
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, max_restarts=3)
    replacements = []

    async def recover():
        replacements.append(True)
        readiness.mark_agent_ready()
        return True

    async def terminate():
        return True

    monkeypatch.setattr(supervisor, "recover_from_failed", recover)
    monkeypatch.setattr(supervisor, "_terminate_failed_worker", terminate)
    supervisor._apply_readiness({"state": "FAILED", "reason": "import error"})
    assert supervisor._automatic_recovery_task is not None
    async with asyncio.timeout(10):
        while not replacements:
            await asyncio.sleep(0.01)
    assert replacements == [True]
    await supervisor.stop()


@pytest.mark.asyncio
async def test_worker_failed_readiness_respects_stop_and_zero_restarts():
    """Stopping the supervisor or disabling restarts must not spawn recovery."""
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, max_restarts=0)
    supervisor._apply_readiness({"state": "FAILED", "reason": "import error"})
    assert readiness.state is ReadinessState.FAILED
    assert supervisor._fatal
    assert supervisor._automatic_recovery_task is None
    await supervisor.stop()

    stopping = Readiness()
    stopped = RuntimeSupervisor(stopping, max_restarts=3)
    stopped._stopping = True
    stopped._apply_readiness({"state": "FAILED", "reason": "import error"})
    assert stopped._automatic_recovery_task is None
    await stopped.stop()
