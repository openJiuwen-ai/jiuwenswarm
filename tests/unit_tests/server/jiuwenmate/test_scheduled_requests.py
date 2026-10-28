"""JM-19: a due plan creates one scheduled Request and does not repeat it after a restart."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.scheduled_requests import employee_conversation_id
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore
from test_session_scheduler import NS, Host

START = datetime(2026, 10, 14, 7, 30, tzinfo=timezone.utc)
HOURLY = "0 * * * *"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def host(tmp_path):
    clock = Clock()
    opened = Host(tmp_path, clock=clock)
    await opened.manager.start()
    try:
        yield opened, clock
    finally:
        await opened.manager.aclose()


def _chat_id(host: Host, agent_id: str = "mate_dev") -> str:
    listed = WebConversationStore(host.sessions.db_path).list(NS, agent_id)
    assert listed
    return listed[0].conversation_id


def _requests(host: Host, plan_id: str):
    session = host.sessions.find_session(NS, "mate_dev", ConversationRef.web(_chat_id(host)))
    assert session is not None
    assert host.sessions.find_session(NS, "mate_dev", ConversationRef.web(f"schedule:{plan_id}")) is None
    return host.sessions.session_requests(NS, session.session_id)


async def test_each_due_occurrence_creates_one_request_and_a_restart_does_not_repeat_it(tmp_path):
    clock = Clock()
    first = Host(tmp_path, clock=clock)
    await first.manager.start()
    try:
        plan = first.manager.schedules.create(
            NS, "mate_dev", name="早报", prompt="整理早报", cron_expr=HOURLY
        )
        assert plan.next_fire_at == datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
        assert first.manager.schedules.list(NS, "mate_dev")[0].plan_id == plan.plan_id

        clock.now = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
        assert await first.manager.schedules.sweep() == 3
        states = _requests(first, plan.plan_id)
        assert [state.record.request_input.kind for state in states] == ["scheduled"] * 3
        assert [state.record.request_input.text for state in states] == ["整理早报"] * 3
        assert len({state.record.session_id for state in states}) == 1
        assert {state.record.source_ref for state in states} == {
            "schedule:%s:2026-10-14T08:00:00+00:00" % plan.plan_id,
            "schedule:%s:2026-10-14T09:00:00+00:00" % plan.plan_id,
            "schedule:%s:2026-10-14T10:00:00+00:00" % plan.plan_id,
        }
        assert all(state.record.route_snapshot is not None and state.record.route_snapshot.channel == "web" for state in states)
        assert await first.manager.schedules.sweep() == 0
        assert len(_requests(first, plan.plan_id)) == 3
    finally:
        await first.manager.aclose()

    restarted = Host(tmp_path, clock=clock)
    await restarted.manager.start()
    try:
        assert await restarted.manager.schedules.sweep() == 0
        assert len(_requests(restarted, plan.plan_id)) == 3
        assert restarted.manager.schedules.list(NS, "mate_dev")[0].next_fire_at == datetime(
            2026, 10, 14, 11, 0, tzinfo=timezone.utc
        )
    finally:
        await restarted.manager.aclose()


async def test_a_disabled_plan_does_not_backfill_occurrences_from_the_pause(host):
    opened, clock = host
    plan = opened.manager.schedules.create(NS, "mate_dev", name="晚报", prompt="整理晚报", cron_expr=HOURLY)
    opened.manager.schedules.set_enabled(NS, plan.plan_id, enabled=False)
    clock.now = START + timedelta(hours=5)
    assert await opened.manager.schedules.sweep() == 0
    assert opened.sessions.find_session(NS, "mate_dev", ConversationRef.web(f"schedule:{plan.plan_id}")) is None
    assert WebConversationStore(opened.sessions.db_path).list(NS, "mate_dev") == []

    resumed = opened.manager.schedules.set_enabled(NS, plan.plan_id, enabled=True)
    assert resumed.next_fire_at > clock.now
    assert await opened.manager.schedules.sweep() == 0


async def test_a_disabled_colleague_skips_due_plans_and_refuses_a_manual_run(host):
    opened, clock = host
    plan = opened.manager.schedules.create(NS, "mate_dev", name="早报", prompt="整理早报", cron_expr=HOURLY)
    opened.service.disabled_agents.add("mate_dev")
    clock.now = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)

    assert await opened.manager.schedules.sweep() == 0
    assert opened.sessions.find_session(NS, "mate_dev", ConversationRef.web(employee_conversation_id("mate_dev"))) is None
    assert opened.manager.schedules.list(NS, "mate_dev")[0].next_fire_at == datetime(
        2026, 10, 14, 11, 0, tzinfo=timezone.utc
    )
    with pytest.raises(PersistentAgentError, match="disabled colleague"):
        await opened.manager.schedules.run(NS, plan.plan_id)


async def test_an_unpublished_colleague_keeps_the_owed_occurrence(host):
    opened, clock = host
    plan = opened.manager.schedules.create(NS, "mate_dev", name="早报", prompt="整理早报", cron_expr=HOURLY)
    opened.service.refuse_snapshot = True
    clock.now = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
    assert await opened.manager.schedules.sweep() == 0
    stored = opened.manager.schedules.list(NS, "mate_dev")[0]
    assert stored.next_fire_at == plan.next_fire_at

    opened.service.refuse_snapshot = False
    assert await opened.manager.schedules.sweep() == 1
    assert len(_requests(opened, plan.plan_id)) == 1


async def test_deleting_a_colleague_removes_the_plan(host):
    opened, _clock = host
    plan = opened.manager.schedules.create(NS, "mate_dev", name="早报", prompt="整理早报", cron_expr=HOURLY)
    opened.manager.schedules.delete_agent(NS, "mate_dev")
    assert opened.manager.schedules.list(NS, "mate_dev") == []
    with pytest.raises(Exception, match="schedule not found"):
        opened.manager.schedules.delete(NS, plan.plan_id)


async def test_interval_window_and_manual_run_keep_periodic_cursor(host):
    opened, clock = host
    service = opened.manager.schedules
    plan = service.create(NS, "mate_dev", name="巡检", prompt="检查", schedule_kind="interval",
                          interval_seconds=60, starts_at=START + timedelta(minutes=2),
                          ends_at=START + timedelta(minutes=4))
    assert plan.next_fire_at == START + timedelta(minutes=2)
    request_id = await service.run(NS, plan.plan_id)
    assert service.list(NS)[0].next_fire_at == plan.next_fire_at
    assert service.history(NS, plan.plan_id)["runs"][0]["request_id"] == request_id
    clock.now = START + timedelta(minutes=5)
    assert await service.sweep() == 3
    assert service.list(NS)[0].next_fire_at is None
    assert service.list(NS)[0].to_dict()["run_count"] == 4


async def test_once_runs_only_once_and_rejects_expired_enable(host):
    opened, clock = host
    service = opened.manager.schedules
    plan = service.create(NS, "mate_dev", name="提醒", prompt="提醒我", schedule_kind="once",
                          run_at=START + timedelta(minutes=1))
    clock.now += timedelta(minutes=2)
    assert await service.sweep() == 1
    assert await service.sweep() == 0
    assert service.list(NS)[0].next_fire_at is None
    with pytest.raises(Exception, match="expired|completed"):
        service.set_enabled(NS, plan.plan_id, enabled=True)


async def test_update_keeps_identity_history_and_uses_new_prompt(host):
    opened, clock = host
    service = opened.manager.schedules
    plan = service.create(NS, "mate_dev", name="旧", prompt="旧内容", cron_expr=HOURLY)
    first = await service.run(NS, plan.plan_id)
    updated = service.update(NS, plan.plan_id, name="新", prompt="新内容", schedule_kind="interval", interval_seconds=60)
    assert updated.plan_id == plan.plan_id
    assert updated.to_dict()["run_count"] == 1
    second = await service.run(NS, plan.plan_id)
    assert first != second
    assert _requests(opened, plan.plan_id)[-1].record.request_input.text == "新内容"
    opened.sessions.finish_request(NS, second, "failed", now=clock.now)
    history = service.history(NS, plan.plan_id)
    assert history["runs"][0]["status"] == "failed"
    assert history["runs"][0]["description"] == "failed"


async def test_a_due_plan_lands_in_each_colleagues_chat(host):
    opened, clock = host
    chats = WebConversationStore(opened.sessions.db_path)
    chats.touch(NS, "mate_dev", "chat-dev", text="你好", now=START)
    chats.touch(NS, "mate_other", "chat-other", text="你好", now=START)
    service = opened.manager.schedules
    dev = service.create(NS, "mate_dev", name="喝水", prompt="提醒喝水", cron_expr=HOURLY)
    other = service.create(NS, "mate_other", name="起身", prompt="提醒起身", cron_expr=HOURLY)
    assert dev.to_dict()["conversation_id"] == "chat-dev"
    assert other.to_dict()["conversation_id"] == "chat-other"

    clock.now = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
    assert await service.sweep() == 2

    def texts(agent_id: str, conversation_id: str) -> list[str]:
        session = opened.sessions.find_session(NS, agent_id, ConversationRef.web(conversation_id))
        assert session is not None
        states = opened.sessions.session_requests(NS, session.session_id)
        assert states[0].record.route_snapshot is not None
        assert states[0].record.route_snapshot.target_id == conversation_id
        assert states[0].record.request_input.reply_target == conversation_id
        return [state.record.request_input.text for state in states]

    assert texts("mate_dev", "chat-dev") == ["提醒喝水"]
    assert texts("mate_other", "chat-other") == ["提醒起身"]
    assert chats.list(NS, "mate_dev")[0].updated_at == clock.now
    assert chats.list(NS, "mate_other")[0].updated_at == clock.now
    assert opened.sessions.find_session(NS, "mate_dev", ConversationRef.web(f"schedule:{dev.plan_id}")) is None
    assert opened.sessions.find_session(NS, "mate_other", ConversationRef.web(f"schedule:{other.plan_id}")) is None
