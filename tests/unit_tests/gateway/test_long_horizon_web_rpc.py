from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.agents.harness.common.long_horizon.core import upsert_long_horizon_task
from jiuwenswarm.agents.harness.common.long_horizon.models import (
    Anchor,
    LongHorizonStage,
    LongHorizonTask,
)
from jiuwenswarm.agents.harness.common.long_horizon.runtime import mark_stage_due
from jiuwenswarm.agents.harness.common.long_horizon.tools import LongHorizonActions
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.gateway.channel_manager.web.long_horizon_web_rpc import (
    register_long_horizon_web_methods,
    run_long_horizon_inbox,
    run_long_horizon_list,
    run_long_horizon_stage_action,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


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


class FakeAgentClient:
    """Stand-in for AgentServer: owns workspace JSON, Gateway only RPCs."""

    def __init__(self, workspace, cron=None) -> None:
        self.workspace = str(workspace)
        self.cron = cron

    async def send_request(self, envelope):
        params = dict(getattr(envelope, "params", None) or {})
        action = str(params.get("action") or "")
        if action == "mark_due":
            result = await mark_stage_due(
                self.workspace,
                str(params.get("task_id") or ""),
                str(params.get("stage_id") or ""),
                job_id=str(params.get("job_id") or ""),
            )
        else:
            result = await LongHorizonActions(
                self.workspace, cron=self.cron
            ).handle(params)
        return AgentResponse(
            request_id=str(getattr(envelope, "request_id", "") or ""),
            channel_id=str(getattr(envelope, "channel", None) or "web"),
            ok=bool(result.get("success")),
            payload=result,
        )


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def fake_cron():
    return FakeCron()


@pytest.fixture
def web_context(workspace, fake_cron):
    return SimpleNamespace(
        agent_client=FakeAgentClient(workspace, cron=fake_cron),
        channel_id="web",
    )


def _seed_due_task(workspace) -> LongHorizonTask:
    task = LongHorizonTask(
        id="lhc_webrpc001",
        title="年度发布",
        kind="generic",
        anchor=Anchor(type="date", month=12, day=20, timezone="Asia/Shanghai"),
        recurrence="once",
        status="active",
        exec_session_id="longhorizon_lhc_webrpc001",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        brief="目标、约束、交付物和关键背景。",
        stages=[
            LongHorizonStage(
                id="lhs_stage001",
                offset_days=-30,
                title="准备材料",
                hint="整理清单",
                plan="整理发布清单并标记负责人。",
                status="due",
                due_at=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc).isoformat(),
                cron_job_id="lh-lhc_webrpc001-lhs_stage001",
                snooze_until="",
                conclusion="",
            ),
            LongHorizonStage(
                id="lhs_stage002",
                offset_days=0,
                title="发布检查",
                hint="",
                plan="逐项核对清单并输出检查结论。",
                status="pending",
                due_at=datetime(2026, 12, 20, 9, 0, tzinfo=timezone.utc).isoformat(),
                cron_job_id="lh-lhc_webrpc001-lhs_stage002",
                snooze_until="",
                conclusion="",
            ),
        ],
    )
    upsert_long_horizon_task(workspace, task)
    return task


@pytest.mark.asyncio
async def test_inbox_rpc_returns_due_stages(web_context, workspace):
    _seed_due_task(workspace)
    response = await run_long_horizon_inbox(web_context, {})
    assert response["type"] == "long_horizon_inbox_result"
    assert response["success"] is True
    assert len(response["inbox"]) == 1
    assert response["inbox"][0]["stage"]["id"] == "lhs_stage001"
    assert response["inbox"][0]["task"]["id"] == "lhc_webrpc001"


@pytest.mark.asyncio
async def test_list_rpc_returns_tasks(web_context, workspace):
    _seed_due_task(workspace)
    response = await run_long_horizon_list(web_context, {})
    assert response["type"] == "long_horizon_list_result"
    assert response["success"] is True
    assert response["count"] == 1
    assert response["tasks"][0]["title"] == "年度发布"


@pytest.mark.asyncio
async def test_stage_action_start_returns_exec_session(web_context, workspace):
    _seed_due_task(workspace)
    response = await run_long_horizon_stage_action(
        web_context,
        {
            "task_id": "lhc_webrpc001",
            "stage_id": "lhs_stage001",
            "stage_action": "start",
        },
    )
    assert response["type"] == "long_horizon_stage_action_result"
    assert response["success"] is True
    assert response["exec_session_id"] == "longhorizon_lhc_webrpc001"
    assert "kick_query" in response and response["kick_query"]
    assert response["task"]["stages"][0]["status"] == "in_progress"
    inbox = await run_long_horizon_inbox(web_context, {})
    assert inbox["inbox"] == []


@pytest.mark.asyncio
async def test_stage_action_skip_updates_stage(web_context, workspace):
    _seed_due_task(workspace)
    response = await run_long_horizon_stage_action(
        web_context,
        {
            "task_id": "lhc_webrpc001",
            "stage_id": "lhs_stage001",
            "stage_action": "skip",
        },
    )
    assert response["success"] is True
    inbox = await run_long_horizon_inbox(web_context, {})
    assert inbox["inbox"] == []


@pytest.mark.asyncio
async def test_web_rpc_fails_without_agent_client():
    response = await run_long_horizon_inbox(SimpleNamespace(), {})
    assert response["success"] is False
    assert response["error"] == "agent_client_unavailable"


@pytest.mark.asyncio
async def test_register_long_horizon_web_methods():
    registered: dict[str, object] = {}

    class FakeChannel:
        def register_method(self, name: str, handler) -> None:
            registered[name] = handler

    register_long_horizon_web_methods(FakeChannel())
    assert "long_horizon_list" in registered
    assert "long_horizon_inbox" in registered
    assert "long_horizon_stage_action" in registered
