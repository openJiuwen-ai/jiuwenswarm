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
async def test_resolve_live_cron_controller_reads_tenant_from_metadata():
    cron = FakeTenantCron()
    registry = FakeRegistry(cron)
    host = SimpleNamespace(_cron_registry=registry, _cron_controller="must-not-use")
    got = await resolve_live_cron_controller(
        host, {"service_id": "svc_a", "agent_id": "ag_b"}
    )
    assert got is cron
    assert registry.calls == [("svc_a", "ag_b")]


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


@pytest.mark.asyncio
async def test_apply_schedule_intent_rolls_back_update_and_create():
    cron = FakeTenantCron()
    cron.jobs["lh-old"] = {"id": "lh-old", "name": "keep-me", "cron_expr": "0 0 9 1 3 * 2027"}
    original_create = cron.create_job

    async def fail_second_create(params: dict):
        job_id = str(params.get("id") or "")
        if job_id.endswith("s2"):
            raise RuntimeError("create failed")
        return await original_create(params)

    cron.create_job = fail_second_create  # type: ignore[method-assign]
    intent = ScheduleIntent.from_dict(
        {
            "op": "replace",
            "task_id": "t1",
            "transactional": True,
            "jobs": [
                {
                    "job_id": "lh-t1-s1",
                    "due_at": "2027-03-01T09:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                    "title": "t",
                    "stage_title": "s1",
                    "tags": {"task_id": "t1", "stage_id": "s1"},
                },
                {
                    "job_id": "lh-t1-s2",
                    "due_at": "2027-03-02T09:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                    "title": "t",
                    "stage_title": "s2",
                    "tags": {"task_id": "t1", "stage_id": "s2"},
                },
            ],
            "remove_job_ids": ["lh-old"],
        }
    )
    with pytest.raises(RuntimeError, match="create failed"):
        await apply_schedule_intent(
            intent, CronControllerBackend(cron), transactional=True
        )
    assert "lh-old" in cron.jobs
    assert cron.jobs["lh-old"]["name"] == "keep-me"
    assert "lh-t1-s1" not in cron.jobs
    assert "lh-t1-s2" not in cron.jobs


def test_should_drop_oneshot_after_mark_due():
    from jiuwenswarm.gateway.long_horizon.job_tags import (
        should_drop_oneshot_after_mark_due,
    )

    assert should_drop_oneshot_after_mark_due({"success": True}) is True
    assert should_drop_oneshot_after_mark_due(
        {"success": False, "error": "task_not_found"}
    ) is True
    assert should_drop_oneshot_after_mark_due(
        {"success": False, "error": "agent_client_unavailable"}
    ) is False
    assert should_drop_oneshot_after_mark_due(
        {"success": False, "error": "stage_already_in_progress"}
    ) is True
    assert should_drop_oneshot_after_mark_due(
        {"success": False, "message": "stage_already_in_progress"}
    ) is False
