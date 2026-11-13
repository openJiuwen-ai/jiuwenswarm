"""Web cron stream regression: one web session per run, no result broadcast.

Extracted from test_cron_scheduler.py before that mirror suite was dropped
in favour of the upstream agent-gateway coverage. This file keeps the one
jiuwenswarm-unique regression (upstream pin 54e5330d has no namesake) plus
the minimal fakes it needs.
"""
from __future__ import annotations

import pytest

from openjiuwen_protocol.e2a.agent_models import AgentResponse, AgentResponseChunk

from agent_gateway.cron.scheduler import CronSchedulerService
from agent_gateway.cron.store import CronJobStore
from agent_gateway.message_handler.message_handler import MessageHandler


class _MessageHandlerStreamTestApi(MessageHandler):
    @classmethod
    def is_terminal_stream_chunk(cls, chunk) -> bool:
        return cls._is_terminal_stream_chunk(chunk)

    @classmethod
    def chunk_to_message(cls, chunk, *, session_id, metadata=None):
        return cls._chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=metadata,
        )

    @classmethod
    def is_permission_resume(cls, env, session_id) -> bool:
        return cls._is_permission_resume(env, session_id)


class _TestableScheduler(CronSchedulerService):
    """Subclass that exposes protected members as public methods.

    G.CLS.11 forbids accessing protected members from outside the class
    hierarchy. By subclassing, we can access them legitimately and then
    expose thin public wrappers for test assertions — no source changes needed.
    """

    async def handle_event(self, ev):
        return await self._handle_event(ev)

    @property
    def runs(self):
        return self._runs

    @property
    def run_tasks(self):
        """Expose _run_tasks for test assertions (G.CLS.11: access via subclass property)."""
        return self._run_tasks

    @property
    def events(self):
        """Expose the queued event heap for test assertions."""
        return self._events

    async def on_wake(self, job, run_id):
        return await self._on_wake(job, run_id)


class FakeAgentClient:
    """Stub AgentServerClient that never calls a real agent."""

    def __init__(self) -> None:
        self.unary_requests = []
        self.stream_requests = []
        self.created_session_id = "cron_agentserver_allocated"

    async def send_request(self, envelope, *a, **kw):
        self.unary_requests.append(envelope)
        if envelope.method == "session.create":
            return AgentResponse(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                ok=True,
                payload={"session_id": self.created_session_id},
            )
        if envelope.method == "project.lifecycle":
            # scheduler.project_execution_allowed 的准入查询：
            # 默认按"项目存在且未归档"放行，归档拦截场景由专项测试覆盖。
            return AgentResponse(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                ok=True,
                payload={"exists": True, "execution_blocked": False},
            )
        return AgentResponse(
            request_id=envelope.request_id or "",
            channel_id=envelope.channel or "",
            ok=True,
            payload={"content": {"output": "done", "result_type": "answer"}},
        )

    async def send_request_stream(self, envelope):
        self.stream_requests.append(envelope)
        payloads = [
            {
                "event_type": "workflow.updated",
                "workflow": {
                    "id": "wf-1",
                    "status": "completed",
                    "summary": "team workflow done",
                },
            },
            {"event_type": "chat.final", "content": "team result"},
            {"is_complete": True},
        ]
        for payload in payloads:
            yield AgentResponseChunk(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                payload=payload,
                is_complete=bool(payload.get("is_complete")),
            )


class FakeMessageHandler:
    """Stub MessageHandler that records published messages."""

    def __init__(self):
        self.published = []
        self.cancel_calls = []

    async def publish_robot_messages(self, msg):
        self.published.append(msg)

    async def register_background_stream(self, envelope, session_id):
        return None

    async def finish_background_stream(self, envelope, session_id):
        return None

    def background_stream_has_other_work(self, request_id, session_id):
        return False

    async def publish_stream_chunk(self, chunk, *, session_id, request_metadata=None):
        if _MessageHandlerStreamTestApi.is_terminal_stream_chunk(chunk):
            return False
        out = _MessageHandlerStreamTestApi.chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=request_metadata,
        )
        await self.publish_robot_messages(out)
        return True

    async def _cancel_agent_work_for_session(self, msg, old_sid, **kwargs):
        self.cancel_calls.append((msg, old_sid, kwargs))


async def _create_one_job(store, name="job", targets="tui", user_id="", mode=None):
    """Convenience: create a single cron job via the store."""
    return await store.create_job(
        name=name,
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets=targets,
        user_id=user_id,
        mode=mode,
    )


def _make_scheduler(store, handler=None, agent_client=None, now_fn=None):
    """Build a _TestableScheduler with fake deps for testing."""
    return _TestableScheduler(
        store=store,
        agent_client=agent_client or FakeAgentClient(),
        message_handler=handler or FakeMessageHandler(),
        **({"now_fn": now_fn} if now_fn is not None else {}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
async def test_web_cron_stream_uses_one_web_session_without_result_broadcast(tmp_path, mode):
    store = CronJobStore(path=tmp_path / "jobs.json")
    job = await _create_one_job(store, targets="web", user_id="owner", mode=mode)
    job.group_id = "group"
    job.bot_id = "bot"
    handler = FakeMessageHandler()
    client = FakeAgentClient()
    client.created_session_id = f"web_allocated_{mode}"
    scheduler = _make_scheduler(store, handler=handler, agent_client=client)
    run_id = f"{job.id}:1234"
    await scheduler.on_wake(job, run_id)
    await scheduler.run_tasks[run_id]
    created = next(env for env in client.unary_requests if env.method == "session.create")
    assert created.channel == "web"
    assert created.params["cron_id"] == job.id
    assert created.channel_context["routing"]["group_id"] == "group"
    assert created.channel_context["routing"]["bot_id"] == "bot"
    assert len(client.stream_requests) == 1
    execution = client.stream_requests[0]
    assert execution.channel == "web"
    assert execution.user_id == "owner"
    assert execution.channel_context["routing"]["group_id"] == "group"
    assert execution.channel_context["routing"]["bot_id"] == "bot"
    assert execution.session_id == scheduler.runs[run_id].exec_session_id
    assert execution.channel_context["cron"]["job_id"] == job.id
    final = [msg for msg in handler.published if (msg.payload or {}).get("event_type") == "chat.final"]
    assert len(final) == 1
    assert final[0].session_id == execution.session_id
    assert final[0].metadata["routing"]["group_id"] == "group"
    for _, _, event in list(scheduler.events):
        if event.kind == "push_update":
            await scheduler.handle_event(event)
    assert len([msg for msg in handler.published if (msg.payload or {}).get("event_type") == "chat.final"]) == 1
    notices = [msg.payload for msg in handler.published if (msg.payload or {}).get("event_type") == "cron.run.updated"]
    assert notices == []
