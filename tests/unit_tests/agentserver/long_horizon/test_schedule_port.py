# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.long_horizon.core import draft_long_horizon_task
from jiuwenswarm.agents.harness.common.long_horizon.schedule_port import (
    LocalSchedulePort,
    ScheduleIntent,
    build_schedule_intent,
    sync_task_schedule,
)
from jiuwenswarm.agents.harness.common.long_horizon.runtime import stage_job_id


class FakeCron:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    async def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    async def create_job(self, *, job_id: str, **payload):
        record = {"id": job_id, **payload}
        self.jobs[job_id] = record
        return record

    async def update_job(self, job_id: str, patch: dict):
        self.jobs[job_id].update(patch)
        return self.jobs[job_id]

    async def delete_job(self, job_id: str, *, force: bool = False):
        self.jobs.pop(job_id, None)
        return True


def _draft_task():
    return draft_long_horizon_task(
        title="考研复习",
        month=3,
        day=15,
        timezone="Asia/Shanghai",
        stages=[
            {
                "title": "背单词",
                "month": 3,
                "day": 1,
                "plan": "每天 50 词",
            },
            {
                "title": "做真题",
                "month": 3,
                "day": 15,
                "plan": "完成两套卷",
            },
        ],
    )


def test_build_schedule_intent_ignore_draft_status():
    task = _draft_task()
    assert task.status == "draft"
    intent = build_schedule_intent(task, ignore_task_status=True)
    assert intent.op == "replace"
    assert len(intent.jobs) == 2
    assert intent.jobs[0].job_id == stage_job_id(task.id, task.stages[0].id)
    assert intent.jobs[0].tags["stage_id"] == task.stages[0].id


def test_build_schedule_intent_draft_without_ignore_removes_all():
    task = _draft_task()
    intent = build_schedule_intent(task, ignore_task_status=False)
    assert intent.jobs == []
    assert len(intent.remove_job_ids) == 2


@pytest.mark.asyncio
async def test_local_port_applies_intent():
    task = _draft_task()
    task.status = "active"
    cron = FakeCron()
    await sync_task_schedule(task, port=LocalSchedulePort(cron))
    assert len(cron.jobs) == 2
    for job in cron.jobs.values():
        assert job.get("session_id") is None
        assert job.get("delete_after_run") is True
        assert "[long_horizon:" in str(job.get("description") or "")


def test_schedule_intent_roundtrip():
    raw = {
        "type": "long_horizon.schedule_intent",
        "op": "upsert",
        "task_id": "lhc_abc",
        "jobs": [
            {
                "job_id": "lh-lhc_abc-lhs_1",
                "due_at": "2026-03-01T09:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "title": "t",
                "stage_title": "s",
                "tags": {"task_id": "lhc_abc", "stage_id": "lhs_1"},
            }
        ],
        "remove_job_ids": [],
    }
    intent = ScheduleIntent.from_dict(raw)
    assert intent.to_dict()["jobs"][0]["job_id"] == "lh-lhc_abc-lhs_1"
