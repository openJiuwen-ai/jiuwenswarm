# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.long_horizon.cron_backend import (
    CronControllerBackend,
    resolve_live_cron_controller,
)
from jiuwenswarm.gateway.long_horizon.schedule_intent import (
    ScheduleIntent,
    apply_schedule_intent,
)
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class FakeTenantCron:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.reloaded = False

    async def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    async def create_job(self, params: dict):
        job_id = str(params.get("id") or "")
        record = dict(params)
        self.jobs[job_id] = record
        return record

    async def update_job(self, job_id: str, patch: dict):
        self.jobs[job_id].update(patch)
        return self.jobs[job_id]

    async def delete_job(self, job_id: str, force: bool = False, skip_ownership: bool = True):
        _ = force, skip_ownership
        self.jobs.pop(job_id, None)
        return True

    async def reload_scheduler(self) -> None:
        self.reloaded = True


class FakeRegistry:
    def __init__(self, controller: FakeTenantCron) -> None:
        self.controller = controller
        self.calls: list[tuple[str, str]] = []

    async def get_controller(self, service_id: str, agent_id: str):
        self.calls.append((service_id, agent_id))
        return self.controller


@pytest.mark.asyncio
async def test_resolve_live_cron_controller_uses_tenant_registry():
    cron = FakeTenantCron()
    registry = FakeRegistry(cron)
    host = SimpleNamespace(_cron_registry=registry, _cron_controller="must-not-use")
    got = await resolve_live_cron_controller(host, None)
    assert got is cron
    assert registry.calls == [("default", "default")]


@pytest.mark.asyncio
async def test_schedule_intent_applies_via_message_handler_registry():
    cron = FakeTenantCron()
    handler = object.__new__(MessageHandler)
    handler._cron_registry = FakeRegistry(cron)
    handler._cron_controller = None
    await handler._handle_long_horizon_schedule_intent(
        payload={
            "type": "long_horizon.schedule_intent",
            "op": "upsert",
            "task_id": "lhc_test",
            "jobs": [
                {
                    "job_id": "lh-lhc_test-lhs_1",
                    "due_at": "2026-09-18T16:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                    "title": "联调验收",
                    "stage_title": "跑通主路径",
                    "tags": {"task_id": "lhc_test", "stage_id": "lhs_1"},
                }
            ],
            "remove_job_ids": [],
            "targets": "web",
        },
        request_id="lh_sched_lhc_test",
        metadata=None,
    )
    assert "lh-lhc_test-lhs_1" in cron.jobs
    assert cron.jobs["lh-lhc_test-lhs_1"].get("session_id") is None
    assert cron.reloaded is True


@pytest.mark.asyncio
async def test_apply_schedule_intent_on_controller_backend():
    cron = FakeTenantCron()
    intent = ScheduleIntent.from_dict(
        {
            "op": "replace",
            "task_id": "t1",
            "jobs": [
                {
                    "job_id": "lh-t1-s1",
                    "due_at": "2026-09-18T16:05:00+08:00",
                    "timezone": "Asia/Shanghai",
                    "title": "t",
                    "stage_title": "s",
                    "tags": {"task_id": "t1", "stage_id": "s1"},
                }
            ],
            "remove_job_ids": [],
        }
    )
    await apply_schedule_intent(intent, CronControllerBackend(cron))
    assert "lh-t1-s1" in cron.jobs
