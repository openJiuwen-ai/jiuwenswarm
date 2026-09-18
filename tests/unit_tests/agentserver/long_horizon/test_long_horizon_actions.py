from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.agents.harness.common.long_horizon.core import (
    get_long_horizon_task,
    load_long_horizon_tasks,
)
from jiuwenswarm.agents.harness.common.long_horizon.runtime import stage_job_id
from jiuwenswarm.agents.harness.common.long_horizon.tools import LongHorizonActions

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
BRIEF = "目标、约束、交付物和关键背景。"


class FakeCron:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.fail_on_create_number: int | None = None
        self._create_count = 0

    async def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    async def create_job(self, *, job_id: str, **payload):
        self._create_count += 1
        if self.fail_on_create_number == self._create_count:
            raise RuntimeError("create failed")
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


def valid_draft_params() -> dict:
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


def confirm_params(task_id: str, *, brief: str = BRIEF) -> dict:
    return {
        "action": "confirm",
        "task_id": task_id,
        "brief": brief,
    }


@pytest.mark.asyncio
async def test_draft_does_not_schedule_but_confirm_schedules_each_stage(
    workspace, fake_cron
):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    assert drafted["success"] is True
    assert fake_cron.jobs == {}
    assert drafted["task"]["status"] == "draft"
    assert drafted["task"]["exec_session_id"] == ""

    confirmed = await actions.handle(
        confirm_params(drafted["task"]["id"])
    )
    assert confirmed["success"] is True
    task = confirmed["task"]
    assert task["status"] == "active"
    assert task["exec_session_id"] == f"longhorizon_{task['id']}"
    assert set(fake_cron.jobs) == {
        stage_job_id(task["id"], stage["id"]) for stage in task["stages"]
    }
    for job in fake_cron.jobs.values():
        # LH cron must not bind longhorizon_* (avoids sidebar cron_id hide).
        assert not job.get("session_id")


@pytest.mark.asyncio
async def test_confirm_rolls_back_jobs_when_a_later_job_fails(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    assert drafted["success"] is True

    fake_cron.fail_on_create_number = 2
    result = await actions.handle(confirm_params(drafted["task"]["id"]))
    assert result["success"] is False
    assert fake_cron.jobs == {}
    stored = load_long_horizon_tasks(workspace)[0]
    assert stored.status == "draft"
    assert stored.id == drafted["task"]["id"]


@pytest.mark.asyncio
async def test_confirm_requires_brief_and_keeps_draft(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    result = await actions.handle(
        {"action": "confirm", "task_id": drafted["task"]["id"], "brief": ""}
    )
    assert result["success"] is False
    assert result["error"] == "brief_required"
    assert fake_cron.jobs == {}
    assert load_long_horizon_tasks(workspace)[0].status == "draft"


@pytest.mark.asyncio
async def test_draft_rejects_missing_stage_plan(workspace, fake_cron):
    params = valid_draft_params()
    params["stages"][0]["plan"] = ""
    result = await LongHorizonActions(workspace, cron=fake_cron).handle(params)
    assert result["success"] is False
    assert result["error"] == "stage_plan_required"
    assert load_long_horizon_tasks(workspace) == []
    assert fake_cron.jobs == {}


@pytest.mark.asyncio
async def test_confirm_is_idempotent_for_active_task(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    first = await actions.handle(confirm_params(drafted["task"]["id"]))
    assert first["success"] is True
    job_ids = set(fake_cron.jobs)

    second = await actions.handle(confirm_params(drafted["task"]["id"]))
    assert second["success"] is True
    assert set(fake_cron.jobs) == job_ids
    assert load_long_horizon_tasks(workspace)[0].status == "active"


@pytest.mark.asyncio
async def test_stage_actions_update_status_and_cron(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    confirmed = await actions.handle(confirm_params(drafted["task"]["id"]))
    task_id = confirmed["task"]["id"]
    stage0 = confirmed["task"]["stages"][0]
    stage1 = confirmed["task"]["stages"][1]
    job0 = stage_job_id(task_id, stage0["id"])
    job1 = stage_job_id(task_id, stage1["id"])
    assert job0 in fake_cron.jobs and job1 in fake_cron.jobs

    pending_later = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage1["id"],
            "stage_action": "start",
        }
    )
    assert pending_later["success"] is False
    assert pending_later["error"] == "stage_not_due"

    # Agent 可提前 start 下一未完成阶段（阶段一仍 pending）。
    early = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage0["id"],
            "stage_action": "start",
        }
    )
    assert early["success"] is True
    assert early["exec_session_id"] == f"longhorizon_{task_id}"
    assert "kick_query" in early and stage0["plan"] in early["kick_query"]
    assert early["task"]["stages"][0]["status"] == "in_progress"
    assert job0 not in fake_cron.jobs
    assert job1 in fake_cron.jobs

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
    snooze_due = snoozed["task"]["stages"][0]["due_at"]
    assert "+08:00" in snooze_due or snooze_due.endswith("+08:00")
    # Must be Asia/Shanghai wall time, not a bare UTC instant re-serialized without offset.
    assert snooze_due.endswith("+08:00") or "+08:00" in snooze_due
    assert "Z" not in snooze_due
    assert job0 in fake_cron.jobs

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

    deferred = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage1["id"],
            "stage_action": "defer",
        }
    )
    assert deferred["success"] is True
    due = datetime.fromisoformat(deferred["task"]["stages"][1]["due_at"])
    original = datetime.fromisoformat(stage1["due_at"])
    assert due - original == timedelta(days=7)
    assert deferred["task"]["stages"][1]["status"] == "pending"
    assert job1 in fake_cron.jobs

    done = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage1["id"],
            "stage_action": "done",
            "conclusion": "检查完成。",
        }
    )
    assert done["success"] is True
    assert done["task"]["stages"][1]["status"] == "done"
    assert job1 not in fake_cron.jobs


@pytest.mark.asyncio
async def test_user_source_can_start_later_pending_stage(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    confirmed = await actions.handle(confirm_params(drafted["task"]["id"]))
    task_id = confirmed["task"]["id"]
    stage0 = confirmed["task"]["stages"][0]
    stage1 = confirmed["task"]["stages"][1]

    blocked = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage1["id"],
            "stage_action": "start",
        }
    )
    assert blocked["success"] is False
    assert blocked["error"] == "stage_not_due"

    jumped = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage1["id"],
            "stage_action": "start",
            "source": "user",
        }
    )
    assert jumped["success"] is True
    assert jumped["task"]["stages"][0]["status"] == "pending"
    assert jumped["task"]["stages"][1]["status"] == "in_progress"
    assert stage0["title"] in jumped["kick_query"]
    assert "前置阶段尚未完成" in jumped["kick_query"]
    assert jumped["exec_session_id"] == f"longhorizon_{task_id}"


@pytest.mark.asyncio
async def test_update_rewrites_stages_keeps_session_and_allows_mid_insert(
    workspace, fake_cron
):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    confirmed = await actions.handle(confirm_params(drafted["task"]["id"]))
    task_id = confirmed["task"]["id"]
    session_id = confirmed["task"]["exec_session_id"]
    assert session_id == f"longhorizon_{task_id}"
    stage0 = confirmed["task"]["stages"][0]
    stage1 = confirmed["task"]["stages"][1]

    # Mark first stage done so rewrite must auto-keep it.
    from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
        deliver_stage_reminder,
    )

    await deliver_stage_reminder(workspace, task_id, stage0["id"])
    await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage0["id"],
            "stage_action": "start",
        }
    )
    await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_id": stage0["id"],
            "stage_action": "done",
            "conclusion": "材料齐了。",
        }
    )

    updated = await actions.handle(
        {
            "action": "update",
            "task_id": task_id,
            "now": NOW.isoformat(),
            "stages": [
                {
                    "title": "中间插：灰度验证",
                    "due_at": "2026-12-01 14:00",
                    "plan": "先小流量验证再全量。",
                },
                {
                    "id": stage1["id"],
                    "title": "发布检查",
                    "due_at": "2026-12-20 09:00",
                    "plan": "逐项核对清单并输出检查结论。",
                },
                {
                    "title": "发布后复盘",
                    "due_at": "2026-12-25 09:00",
                    "plan": "汇总问题与改进项。",
                },
            ],
        }
    )
    assert updated["success"] is True
    task = updated["task"]
    assert task["id"] == task_id
    assert task["exec_session_id"] == session_id
    assert updated["exec_session_id"] == session_id

    titles = [stage["title"] for stage in task["stages"]]
    # done stage auto-kept at front even if omitted from rewrite payload
    assert titles == [
        "准备材料",
        "中间插：灰度验证",
        "发布检查",
        "发布后复盘",
    ]
    assert task["stages"][0]["status"] == "done"
    assert task["stages"][0]["id"] == stage0["id"]
    assert task["stages"][2]["id"] == stage1["id"]

    for job in fake_cron.jobs.values():
        assert not job.get("session_id")
    stored = get_long_horizon_task(workspace, task_id)
    assert stored is not None
    assert stored.exec_session_id == session_id
    assert stored.id == task_id


@pytest.mark.asyncio
async def test_update_rejects_draft_and_requires_stages(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    rejected = await actions.handle(
        {
            "action": "update",
            "task_id": drafted["task"]["id"],
            "stages": [
                {
                    "title": "仅一项",
                    "due_at": "2026-12-20 09:00",
                    "plan": "做完它。",
                }
            ],
        }
    )
    assert rejected["success"] is False
    assert rejected["error"] == "task_not_active"

    confirmed = await actions.handle(confirm_params(drafted["task"]["id"]))
    missing = await actions.handle(
        {"action": "update", "task_id": confirmed["task"]["id"]}
    )
    assert missing["success"] is False
    assert missing["error"] == "stages_required"


@pytest.mark.asyncio
async def test_mute_year_and_delete_cancel_jobs(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    confirmed = await actions.handle(confirm_params(drafted["task"]["id"]))
    task_id = confirmed["task"]["id"]
    assert fake_cron.jobs

    muted = await actions.handle(
        {
            "action": "stage_action",
            "task_id": task_id,
            "stage_action": "mute_year",
        }
    )
    assert muted["success"] is True
    assert muted["task"]["status"] == "muted_year"
    assert fake_cron.jobs == {}

    # re-confirm path not required; create another task for delete
    drafted2 = await actions.handle(valid_draft_params())
    confirmed2 = await actions.handle(confirm_params(drafted2["task"]["id"]))
    task2 = confirmed2["task"]["id"]
    assert fake_cron.jobs

    deleted = await actions.handle({"action": "delete", "task_id": task2})
    assert deleted["success"] is True
    assert get_long_horizon_task(workspace, task2) is None
    assert not any(task2 in job_id for job_id in fake_cron.jobs)


@pytest.mark.asyncio
async def test_list_and_inbox(workspace, fake_cron):
    actions = LongHorizonActions(workspace, cron=fake_cron)
    drafted = await actions.handle(valid_draft_params())
    confirmed = await actions.handle(confirm_params(drafted["task"]["id"]))
    task_id = confirmed["task"]["id"]
    stage_id = confirmed["task"]["stages"][0]["id"]

    listed = await actions.handle({"action": "list"})
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["tasks"][0]["id"] == task_id

    empty_inbox = await actions.handle({"action": "inbox"})
    assert empty_inbox["success"] is True
    assert empty_inbox["inbox"] == []

    from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
        deliver_stage_reminder,
    )

    delivered = await deliver_stage_reminder(workspace, task_id, stage_id)
    assert delivered["success"] is True
    assert delivered.get("event_type") == "long_horizon.stage_due"

    # idempotent due delivery
    again = await deliver_stage_reminder(workspace, task_id, stage_id)
    assert again["success"] is True

    inbox = await actions.handle({"action": "inbox"})
    assert inbox["success"] is True
    assert len(inbox["inbox"]) == 1
    assert inbox["inbox"][0]["stage"]["id"] == stage_id
    assert inbox["inbox"][0]["stage"]["status"] == "due"


@pytest.mark.asyncio
async def test_stage_job_id_contract():
    assert stage_job_id("lhc_abc", "lhs_xyz") == "lh-lhc_abc-lhs_xyz"
