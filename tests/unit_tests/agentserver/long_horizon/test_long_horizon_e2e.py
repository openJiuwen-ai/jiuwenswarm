"""End-to-end regression for long-horizon confirm → due → start / snooze → skip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.agents.harness.common.long_horizon.core import (
    get_long_horizon_task,
    list_inbox,
)
from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
    deliver_from_cron_job,
    deliver_stage_reminder,
    stage_job_id,
)
from jiuwenswarm.agents.harness.common.long_horizon.tools import LongHorizonActions

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
BRIEF = "目标、约束、交付物和关键背景。"


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
        if job_id not in self.jobs:
            raise KeyError(job_id)
        self.jobs[job_id].update(patch)
        return self.jobs[job_id]

    async def delete_job(self, job_id: str, *, force: bool = False):
        self.jobs.pop(job_id, None)
        return True


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def fake_cron():
    return FakeCron()


def _two_stage_draft_params() -> dict:
    return {
        "action": "draft",
        "title": "年度发布",
        "month": 12,
        "day": 20,
        "timezone": "Asia/Shanghai",
        "stages": [
            {
                "title": "准备材料",
                "month": 11,
                "day": 20,
                "plan": "整理发布清单并标记负责人。",
            },
            {
                "title": "发布检查",
                "month": 12,
                "day": 20,
                "plan": "逐项核对清单并输出检查结论。",
            },
        ],
        "now": NOW.isoformat(),
    }


@pytest.mark.asyncio
async def test_e2e_two_stage_confirm_cron_due_start(workspace, fake_cron):
    """Scenario 1: confirm → two Cron jobs → stage due → start into exec session."""
    actions = LongHorizonActions(workspace, cron=fake_cron)

    drafted = await actions.handle(_two_stage_draft_params())
    assert drafted["success"] is True
    assert fake_cron.jobs == {}

    confirmed = await actions.handle(
        {
            "action": "confirm",
            "task_id": drafted["task"]["id"],
            "brief": BRIEF,
        }
    )
    assert confirmed["success"] is True
    task = confirmed["task"]
    task_id = task["id"]
    stage0, stage1 = task["stages"]
    job0 = stage_job_id(task_id, stage0["id"])
    job1 = stage_job_id(task_id, stage1["id"])
    exec_session = f"longhorizon_{task_id}"

    assert task["status"] == "active"
    assert task["exec_session_id"] == exec_session
    assert set(fake_cron.jobs) == {job0, job1}
    assert not fake_cron.jobs[job0].get("session_id")
    assert not fake_cron.jobs[job1].get("session_id")

    due = await deliver_from_cron_job(
        fake_cron.jobs[job0], workspace=workspace
    )
    assert due["success"] is True
    assert due["event_type"] == "long_horizon.stage_due"
    assert due["task_id"] == task_id
    assert due["stage_id"] == stage0["id"]
    assert due["exec_session_id"] == exec_session

    inbox = list_inbox(workspace)
    assert len(inbox) == 1
    assert inbox[0]["stage"]["id"] == stage0["id"]
    assert inbox[0]["stage"]["status"] == "due"

    stored = get_long_horizon_task(workspace, task_id)
    assert stored is not None
    assert stored.stages[0].status == "due"
    assert stored.stages[1].status == "pending"

    started = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage0["id"],
            "stage_action": "start",
        }
    )
    assert started["success"] is True
    assert started["exec_session_id"] == exec_session
    assert "kick_query" in started
    assert BRIEF[:8] in started["kick_query"] or "目标" in started["kick_query"]
    assert stage0["plan"] in started["kick_query"]
    # Start marks stage in_progress and drops its one-shot cron so it won't
    # reappear as expired / re-toast on refresh.
    assert started["task"]["stages"][0]["status"] == "in_progress"
    assert set(fake_cron.jobs) == {job1}


@pytest.mark.asyncio
async def test_e2e_snooze_due_skip_preserves_second_stage_cron(
    workspace, fake_cron
):
    """Scenario 2: snooze rebuilds stage Cron; skip removes it; stage2 Cron stays."""
    actions = LongHorizonActions(workspace, cron=fake_cron)

    drafted = await actions.handle(_two_stage_draft_params())
    confirmed = await actions.handle(
        {
            "action": "confirm",
            "task_id": drafted["task"]["id"],
            "brief": BRIEF,
        }
    )
    assert confirmed["success"] is True
    task_id = confirmed["task"]["id"]
    stage0 = confirmed["task"]["stages"][0]
    stage1 = confirmed["task"]["stages"][1]
    job0 = stage_job_id(task_id, stage0["id"])
    job1 = stage_job_id(task_id, stage1["id"])
    stage1_cron_before = dict(fake_cron.jobs[job1])
    original_expr = fake_cron.jobs[job0]["cron_expr"]

    # First due (as Toast would see)
    due1 = await deliver_stage_reminder(workspace, task_id, stage0["id"])
    assert due1["success"] is True
    assert due1["event_type"] == "long_horizon.stage_due"

    snoozed = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage0["id"],
            "stage_action": "snooze",
            "snooze_hours": 2,
        }
    )
    assert snoozed["success"] is True
    assert snoozed["task"]["stages"][0]["status"] == "snoozed"
    assert job0 in fake_cron.jobs
    assert job1 in fake_cron.jobs
    # Snooze must refresh the stage Cron schedule (replace old fire time).
    assert fake_cron.jobs[job0]["cron_expr"] != original_expr
    # Second stage Cron untouched.
    assert fake_cron.jobs[job1]["cron_expr"] == stage1_cron_before["cron_expr"]
    assert fake_cron.jobs[job1]["session_id"] == stage1_cron_before["session_id"]

    due_at = datetime.fromisoformat(snoozed["task"]["stages"][0]["due_at"])
    assert due_at.tzinfo is not None
    now_utc = datetime.now(timezone.utc)
    assert now_utc + timedelta(hours=1) < due_at < now_utc + timedelta(hours=3)

    # Second due after snooze window
    due2 = await deliver_from_cron_job(
        fake_cron.jobs[job0], workspace=workspace
    )
    assert due2["success"] is True
    assert due2["event_type"] == "long_horizon.stage_due"
    assert get_long_horizon_task(workspace, task_id).stages[0].status == "due"

    skipped = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage0["id"],
            "stage_action": "skip",
            "conclusion": "本阶段暂不处理。",
        }
    )
    assert skipped["success"] is True
    assert skipped["task"]["stages"][0]["status"] == "skipped"
    assert job0 not in fake_cron.jobs
    assert job1 in fake_cron.jobs
    assert fake_cron.jobs[job1]["cron_expr"] == stage1_cron_before["cron_expr"]
    assert skipped["task"]["stages"][1]["status"] == "pending"
    assert list_inbox(workspace) == []
