# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron execution history must survive without its AgentServer Pod or a Web tab."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.channels.web.history_store import api as history_api
from jiuwenswarm.channels.web.history_store.store import ChatHistoryStore
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.gateway.cron import scheduler as scheduler_module
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
from jiuwenswarm.gateway.cron.store import CronJobStore


class HistoryScheduler(CronSchedulerService):
    async def execute(self, job: CronJob, run_id: str):
        await self._on_wake(job, run_id)
        await self._run_tasks[run_id]
        return self._runs[run_id]


class AgentClient:
    def __init__(self, outcome: str):
        self.outcome = outcome
        self.requests = []

    async def send_request(self, envelope):
        self.requests.append(envelope)
        if envelope.method == "session.create":
            return AgentResponse(envelope.request_id, envelope.channel, True,
                                 {"session_id": "cron-history-test"})
        if self.outcome == "exception":
            raise RuntimeError("Agent connection closed")
        if self.outcome == "cancelled":
            raise asyncio.CancelledError
        ok = self.outcome == "success"
        payload = {"content": "晴，25℃"} if ok else {"error": "weather request failed"}
        return AgentResponse(envelope.request_id, envelope.channel, ok, payload)


@pytest.fixture
def history_store(monkeypatch):
    store = ChatHistoryStore.memory()
    monkeypatch.setattr(history_api, "_default_store", store)
    monkeypatch.setattr(scheduler_module, "is_remote_storage", lambda: True)
    return store


def make_scheduler(tmp_path, outcome="success", targets="web"):
    job = CronJob(id="weather", name="查询天气", description="查询北京天气", enabled=True,
                  cron_expr="* * * * *", timezone="Asia/Shanghai", targets=targets,
                  user_id="owner", group_id="group", bot_id="bot", project_id="project",
                  work_mode="work")
    scheduler = HistoryScheduler(store=CronJobStore(path=tmp_path / "jobs.json"),
                                 agent_client=AgentClient(outcome), message_handler=object())
    return scheduler, job


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
@pytest.mark.parametrize("targets", ["web", "tui"])
async def test_completed_execution_saves_owned_body_without_push(
    tmp_path, history_store, outcome, targets,
):
    scheduler, job = make_scheduler(tmp_path, outcome, targets)
    run_id = f"weather:{int(time.time())}"
    state = await scheduler.execute(job, run_id)
    detail = history_store.get_session_detail_blocking(
        state.exec_session_id, user="owner", group_id="group", bot_id="bot",
    )
    assert detail is not None
    assert detail["cron_id"] == "weather"
    assert detail["project_id"] == "project"
    assert detail["message_count"] == 2
    messages = detail["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "查询北京天气"
    assert messages[1]["content"] == state.result_text
    assert messages[1]["event_type"] == ("chat.final" if outcome == "success" else "chat.error")
    assert all(message["request_id"] == f"cron-{run_id}" for message in messages)
    assert history_store.get_session_detail_blocking(state.exec_session_id, user="other") is None
    # A duplicate wake must not add duplicate messages or rerun the task.
    await scheduler.execute(job, run_id)
    assert history_store.get_session_detail_blocking(state.exec_session_id, user="owner")["message_count"] == 2


@pytest.mark.asyncio
async def test_cancelled_execution_does_not_save_or_deliver_body(tmp_path, history_store):
    scheduler, job = make_scheduler(tmp_path, "cancelled")
    with pytest.raises(asyncio.CancelledError):
        await scheduler.execute(job, f"weather:{int(time.time())}")
    detail = history_store.get_session_detail_blocking("cron-history-test", user="owner")
    assert detail is not None
    assert detail["messages"] == []


@pytest.mark.asyncio
async def test_history_db_failure_does_not_change_execution_result(tmp_path, history_store, monkeypatch):
    monkeypatch.setattr(history_store, "record_assistant", AsyncMock(side_effect=RuntimeError("DB unavailable")))
    scheduler, job = make_scheduler(tmp_path)
    state = await scheduler.execute(job, f"weather:{int(time.time())}")
    assert state.status == "succeeded"
    assert state.result_text == "晴，25℃"


@pytest.mark.asyncio
async def test_local_mode_does_not_write_remote_history(tmp_path, history_store, monkeypatch):
    monkeypatch.setattr(scheduler_module, "is_remote_storage", lambda: False)
    scheduler, job = make_scheduler(tmp_path)
    await scheduler.execute(job, f"weather:{int(time.time())}")
    assert history_store.list_sessions_blocking(user="owner", limit=20, offset=0) == []
