# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron execution history must survive without its AgentServer Pod or a Web tab."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.channels.web.history_store import api as history_api
from jiuwenswarm.channels.web.history_store.store import ChatHistoryStore
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.gateway.cron import scheduler as scheduler_module
from jiuwenswarm.gateway.cron.models import CronJob, CronRunState
from jiuwenswarm.gateway.cron.scheduler import (
    CronSchedulerService,
    _persist_remote_cron_history,
)
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

    async def send_request_stream(self, envelope):
        """Agent 模式 chat.send 已改走流式；结果语义与上面的 unary 桩一致。"""
        self.requests.append(envelope)
        if self.outcome == "exception":
            raise RuntimeError("Agent connection closed")
        if self.outcome == "cancelled":
            raise asyncio.CancelledError
        ok = self.outcome == "success"
        payload = (
            {"event_type": "chat.final", "content": "晴，25℃"}
            if ok
            else {"event_type": "chat.error", "error": "weather request failed"}
        )
        yield AgentResponseChunk(
            request_id=envelope.request_id or "",
            channel_id=envelope.channel or "",
            payload=payload,
            is_complete=True,
        )


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


@pytest.mark.asyncio
async def test_awaiting_approval_persists_user_prompt_without_assistant(
    tmp_path, history_store,
):
    """审批暂停时 result_text 为空，仍须落 user 任务内容，刷新后窗口不空白。"""
    job = CronJob(
        id="bash-ask",
        name="试 bash",
        description="尝试调用一下bash工具",
        enabled=True,
        cron_expr="* * * * *",
        timezone="Asia/Shanghai",
        targets="web",
        user_id="owner",
        group_id="group",
        bot_id="bot",
        project_id="project",
        work_mode="work",
    )
    run_id = f"bash-ask:{int(time.time())}"
    state = CronRunState(
        run_id=run_id,
        job_id=job.id,
        wake_at_iso="2026-10-13T15:01:00+08:00",
        push_at_iso="2026-10-13T15:01:00+08:00",
        job_name=job.name,
        targets=job.targets,
        session_id="",
        chat_type="",
        timezone=job.timezone,
        exec_session_id="cron_awaiting_hist_1",
        status="awaiting_approval",
        started_at=time.time(),
        finished_at=time.time(),
        result_text=None,
    )
    await _persist_remote_cron_history(job, state)
    detail = history_store.get_session_detail_blocking(
        state.exec_session_id, user="owner", group_id="group", bot_id="bot",
    )
    assert detail is not None
    assert detail["message_count"] == 1
    assert detail["messages"][0]["role"] == "user"
    assert detail["messages"][0]["content"] == "尝试调用一下bash工具"
    assert detail["messages"][0]["request_id"] == f"cron-{run_id}"


@pytest.mark.asyncio
async def test_permission_pause_stream_persists_user_and_run_now_reuses_session(
    tmp_path, history_store,
):
    """端到端：流式审批暂停落 user；再次 run_now 复用同一 cron_*。"""

    class AskStreamClient(AgentClient):
        def __init__(self):
            super().__init__("success")

        async def send_request_stream(self, envelope):
            self.requests.append(envelope)
            payloads = [
                {"event_type": "chat.ask_user_question", "request_id": "perm-1"},
                {"event_type": "chat.invocation_paused", "awaiting_user_input": True},
            ]
            for payload in payloads:
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload=payload,
                    is_complete=payload.get("event_type") == "chat.invocation_paused",
                )

    class FakeHandler:
        def __init__(self):
            self.published = []

        async def publish_robot_messages(self, msg):
            self.published.append(msg)

    job = CronJob(
        id="bash-reuse",
        name="试 bash",
        description="尝试调用一下bash工具",
        enabled=True,
        cron_expr="* * * * *",
        timezone="Asia/Shanghai",
        targets="web",
        user_id="owner",
        group_id="group",
        bot_id="bot",
        project_id="project",
        work_mode="work",
        mode="agent",
    )
    store = CronJobStore(path=tmp_path / "jobs.json")
    agent = AskStreamClient()
    scheduler = CronSchedulerService(
        store=store, agent_client=agent, message_handler=FakeHandler(),
    )
    scheduler._jobs[job.id] = job

    run_id = f"{job.id}:1001"
    await scheduler._on_wake(job, run_id)
    task = scheduler._run_tasks.get(run_id)
    assert task is not None
    await task

    state = scheduler._runs[run_id]
    assert state.status == "awaiting_approval"
    assert scheduler._permission_hold_active(job.id)

    detail = history_store.get_session_detail_blocking(
        state.exec_session_id, user="owner", group_id="group", bot_id="bot",
    )
    assert detail is not None
    assert detail["message_count"] == 1
    assert detail["messages"][0]["content"] == "尝试调用一下bash工具"

    reused = await scheduler.trigger_run_now_info(job.id, preallocate=True)
    assert reused["session_id"] == state.exec_session_id
    assert reused.get("reused_pending_session") is True
    assert reused.get("awaiting_approval") is True
    assert reused["run_id"] == run_id
    # 复用路径不再 schedule 新 wake / 新 session.create
    assert len([r for r in agent.requests if getattr(r, "method", None) == "session.create"]) == 1
