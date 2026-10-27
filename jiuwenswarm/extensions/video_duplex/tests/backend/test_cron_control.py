"""Scheduled-job tools belong to the text queue manager, never to the realtime model."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import cron_control, queue_manager
from jiuwenswarm.extensions.video_duplex.backend.cron_control import (
    CRON_DELETE_TOOL,
    CRON_QUERY_TOOL,
    CRON_UPDATE_TOOL,
    CronControl,
    parse_cron_arguments,
)
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import parse_qwen_omni_tool_call


def job(job_id="job-1", *, user_id="alice", mode="agent", enabled=True):
    return {
        "id": job_id,
        "name": "早间提醒",
        "enabled": enabled,
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "description": "提醒喝水",
        "targets": "web",
        "mode": mode,
        "project_id": "",
        "user_id": user_id,
    }


class FakeCronController:
    """Keep only what the voice tools are allowed to call."""

    def __init__(self, jobs):
        self.jobs = {item["id"]: dict(item) for item in jobs}
        self.updated = []
        self.deleted = []
        self.previewed = []

    async def list_jobs(self, user_id=None):
        values = list(self.jobs.values())
        if user_id:
            values = [item for item in values if item.get("user_id") == user_id]
        return [dict(item) for item in values]

    async def get_job(self, job_id):
        item = self.jobs.get(job_id)
        return dict(item) if item else None

    async def preview_job(self, job_id, count=5):
        self.previewed.append((job_id, count))
        return [{"wake_at": "2026-10-28T08:59:00+08:00", "push_at": "2026-10-28T09:00:00+08:00"}]

    async def update_job(self, job_id, patch):
        self.jobs[job_id].update(patch)
        self.updated.append((job_id, dict(patch)))
        return dict(self.jobs[job_id])

    async def delete_job(self, job_id):
        self.deleted.append(job_id)
        return self.jobs.pop(job_id, None) is not None


@pytest.mark.asyncio
async def test_query_lists_only_owned_jobs_and_never_applies():
    controller = FakeCronController([job("alice-job"), job("bob-job", user_id="bob")])
    control = CronControl("alice", controller)

    receipt = await control.execute(CRON_QUERY_TOOL, {})

    assert receipt["applied"] is False
    assert receipt["total"] == 1
    assert [item["id"] for item in receipt["jobs"]] == ["alice-job"]
    assert receipt["jobs"][0]["next_push_at"].endswith("+08:00")
    assert controller.updated == [] and controller.deleted == []


@pytest.mark.asyncio
async def test_query_by_job_id_adds_next_runs_and_marks_protected_jobs():
    controller = FakeCronController([job("daily"), job("system", mode="proactive.tick")])
    control = CronControl("alice", controller)

    receipt = await control.execute(CRON_QUERY_TOOL, {"job_id": "system"})

    assert receipt["job"]["protected"] is True
    assert receipt["job"]["next_runs"] == ["2026-10-28T09:00:00+08:00"]
    assert controller.previewed == [("system", 5)]


@pytest.mark.asyncio
async def test_query_never_returns_another_users_job():
    controller = FakeCronController([job("bob-job", user_id="bob")])
    control = CronControl("alice", controller)

    receipt = await control.execute(CRON_QUERY_TOOL, {"job_id": "bob-job"})

    assert receipt["applied"] is False
    assert receipt["code"] == "CRON_JOB_NOT_FOUND"
    assert "job" not in receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("job_owner", ["alice", ""])
async def test_anonymous_control_can_read_but_cannot_change_jobs(job_owner):
    controller = FakeCronController([job(user_id=job_owner)])
    control = CronControl("", controller)

    queried = await control.execute(CRON_QUERY_TOOL, {"job_id": "job-1"})
    updated = await control.execute(
        CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"enabled": False}},
    )
    deleted = await control.execute(CRON_DELETE_TOOL, {"job_id": "job-1"})

    assert queried["job"]["id"] == "job-1"
    assert updated["code"] == deleted["code"] == "CRON_JOB_NOT_FOUND"
    assert updated["applied"] is deleted["applied"] is False
    assert controller.updated == [] and controller.deleted == []
    assert controller.jobs["job-1"]["enabled"] is True


@pytest.mark.asyncio
async def test_update_applies_only_the_whitelisted_patch():
    controller = FakeCronController([job()])
    control = CronControl("alice", controller)

    receipt = await control.execute(
        CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"enabled": False, "cron_expr": "30 8 * * *"}},
    )

    assert receipt["applied"] is True
    assert receipt["changed"] == ["cron_expr", "enabled"]
    assert controller.updated == [("job-1", {"enabled": False, "cron_expr": "30 8 * * *"})]
    assert receipt["job"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, False, True, "job-1", {}])
async def test_invalid_update_result_returns_unsuccessful_receipt(monkeypatch, result):
    controller = FakeCronController([job()])

    async def update_without_receipt(job_id, patch):
        controller.jobs[job_id].update(patch)
        return result

    monkeypatch.setattr(controller, "update_job", update_without_receipt)
    receipt = await CronControl("alice", controller).execute(
        CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"enabled": False}},
    )

    assert receipt["state"] == "failed"
    assert receipt["code"] == "CRON_OPERATION_FAILED"
    assert receipt["applied"] is False
    assert "did not complete successfully" in receipt["message"]
    assert "final state is unconfirmed" in receipt["message"]
    assert "job" not in receipt
    assert controller.jobs["job-1"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("write_applied", [False, True])
async def test_update_exception_returns_unsuccessful_receipt(monkeypatch, write_applied):
    controller = FakeCronController([job()])

    async def failed_update(job_id, patch):
        if write_applied:
            controller.jobs[job_id].update(patch)
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(controller, "update_job", failed_update)
    receipt = await CronControl("alice", controller).execute(
        CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"enabled": False}},
    )

    assert receipt["state"] == "failed"
    assert receipt["applied"] is False
    assert receipt["error"] == "scheduler unavailable"
    assert "final state is unconfirmed" in receipt["message"]
    assert controller.jobs["job-1"]["enabled"] is not write_applied


@pytest.mark.asyncio
async def test_update_refuses_protected_jobs():
    controller = FakeCronController([job("system", mode="proactive.tick")])
    control = CronControl("alice", controller)

    receipt = await control.execute(CRON_UPDATE_TOOL, {"job_id": "system", "patch": {"enabled": False}})

    assert receipt["code"] == "CRON_JOB_PROTECTED"
    assert controller.updated == []


@pytest.mark.asyncio
async def test_delete_requires_ownership_and_records_the_removal():
    controller = FakeCronController([job("mine"), job("theirs", user_id="bob")])
    control = CronControl("alice", controller)

    rejected = await control.execute(CRON_DELETE_TOOL, {"job_id": "theirs"})
    applied = await control.execute(CRON_DELETE_TOOL, {"job_id": "mine"})

    assert rejected["code"] == "CRON_JOB_NOT_FOUND"
    assert applied["applied"] is True and applied["deleted"] is True
    assert controller.deleted == ["mine"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (CRON_QUERY_TOOL, {"job_id": "job-1", "extra": 1}),
        (CRON_QUERY_TOOL, {"limit": 0}),
        (CRON_QUERY_TOOL, {"limit": True}),
        (CRON_UPDATE_TOOL, {"job_id": "job-1"}),
        (CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {}}),
        (CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"project_id": "p"}}),
        (CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"enabled": "false"}}),
        (CRON_UPDATE_TOOL, {"job_id": "job-1", "patch": {"timeout_seconds": 10}}),
        (CRON_DELETE_TOOL, {}),
        (CRON_DELETE_TOOL, {"job_id": "job-1", "force": True}),
    ],
)
def test_cron_arguments_are_validated_strictly(name, arguments):
    with pytest.raises(ValueError):
        parse_cron_arguments(name, arguments)


@pytest.mark.asyncio
async def test_unavailable_cron_returns_a_receipt_instead_of_raising(monkeypatch):
    from jiuwenswarm.gateway.cron import controller as controller_module

    def unavailable(**kwargs):
        raise RuntimeError("CronController not initialized")

    monkeypatch.setattr(controller_module.CronController, "get_instance", unavailable)
    receipt = await CronControl("alice").execute(CRON_QUERY_TOOL, {})

    assert receipt["code"] == "CRON_UNAVAILABLE"
    assert receipt["applied"] is False


def management_call():
    return parse_qwen_omni_tool_call({
        "name": "jiuwen_queue_manage", "call_id": "manage-cron",
        "arguments": {"instruction": "把早上九点的提醒改到八点半"},
    })


@pytest.mark.asyncio
async def test_manager_queries_before_writing_and_reports_real_receipts(monkeypatch):
    controller = FakeCronController([job()])
    control = CronControl("alice", controller)
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    step = AsyncMock(side_effect=[
        json.dumps({"tool": {"name": CRON_UPDATE_TOOL, "arguments": {
            "job_id": "job-1", "patch": {"cron_expr": "30 8 * * *"}}}}),
        json.dumps({"tool": {"name": CRON_QUERY_TOOL, "arguments": {}}}),
        json.dumps({"tool": {"name": CRON_UPDATE_TOOL, "arguments": {
            "job_id": "job-1", "patch": {"cron_expr": "30 8 * * *"}}}}),
        json.dumps({"response": "提醒已改到八点半。"}),
    ])
    monkeypatch.setattr(queue_manager, "request_step", step)

    result = await queue_manager.manage_queue(
        manager, "alice", "voice", management_call(), "把早上九点的提醒改到八点半", cron=control,
    )

    assert result["response"] == "提醒已改到八点半。"
    # The bootstrap task query stays in the trail; the cron steps follow it.
    receipts = [item["receipt"] for item in result["receipts"]]
    assert receipts[0]["jobs"] == []
    assert receipts[-3]["code"] == "CRON_QUERY_REQUIRED"
    assert receipts[-2]["operation"] == CRON_QUERY_TOOL
    assert receipts[-1]["applied"] is True
    assert controller.updated == [("job-1", {"cron_expr": "30 8 * * *"})]


@pytest.mark.asyncio
async def test_manager_exposes_cron_tools_to_the_model(monkeypatch):
    control = CronControl("alice", FakeCronController([]))
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    step = AsyncMock(return_value=json.dumps({"response": "当前没有定时任务。"}))
    monkeypatch.setattr(queue_manager, "request_step", step)

    await queue_manager.manage_queue(
        manager, "alice", "voice", management_call(), "我有哪些定时任务", cron=control,
    )

    context = json.loads(step.call_args.args[0][1]["content"])
    names = {tool["function"]["name"] for tool in context["allowed_tools"]}
    assert {CRON_QUERY_TOOL, CRON_UPDATE_TOOL, CRON_DELETE_TOOL} <= names


@pytest.mark.asyncio
async def test_manager_answers_cron_creation_with_a_delegation_hint(monkeypatch):
    controller = FakeCronController([])
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    step = AsyncMock(side_effect=[
        json.dumps({"tool": {"name": "jiuwen_cron_create", "arguments": {
            "name": "喝水提醒", "cron_expr": "0 9 * * *", "timezone": "Asia/Shanghai",
            "description": "提醒喝水"}}}),
        json.dumps({"response": "新建定时任务需要作为新任务提交。"}),
    ])
    monkeypatch.setattr(queue_manager, "request_step", step)

    result = await queue_manager.manage_queue(
        manager, "alice", "voice", management_call(), "每天九点提醒我喝水",
        cron=CronControl("alice", controller),
    )

    assert result["response"] == "新建定时任务需要作为新任务提交。"
    assert result["receipts"][-1]["receipt"]["code"] == "CRON_CREATE_REQUIRES_DELEGATION"
    assert controller.jobs == {}


@pytest.mark.asyncio
async def test_manager_hides_cron_tools_without_an_executor(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    step = AsyncMock(return_value=json.dumps({"response": "没有可用操作。"}))
    monkeypatch.setattr(queue_manager, "request_step", step)

    await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "定时任务")

    names = {tool["function"]["name"] for tool in json.loads(step.call_args.args[0][1]["content"])["allowed_tools"]}
    assert not names & cron_control.CRON_TOOL_NAMES
