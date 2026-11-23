"""Known group membership stays current without adaptive message polling."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_group_polling import DingTalkGroupPoller
from tests.unit_tests.server.jiuwenmate.test_dingtalk_group_events import NOW, host
from tests.unit_tests.server.jiuwenmate.test_group_directory_attachment import History, directory_payload, invoke


def fixture(tmp_path, *, mode="passive", count=1):
    h = host(tmp_path)
    h.inbound._service.read_settings = MagicMock(return_value=SimpleNamespace(group_reply_mode=mode))
    h.manager.service = h.inbound._service
    h.manager._sessions = SimpleNamespace(db_path=tmp_path / "agents.sqlite3")
    groups = []
    for index in range(count):
        group = h.groups.resolve("local", "dingtalk", f"cid-{index}", now=NOW)
        h.groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=NOW,
                                message_time=NOW, source_ref="dingtalk:message:first")
        groups.append(group)
    clock = [NOW + timedelta(minutes=1)]
    poller = DingTalkGroupPoller(h.manager, h.inbound, config_loader=lambda: h.config, clock=lambda: clock[0])
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["passive", "adaptive"])
async def test_bot_absence_removes_group_without_message_reads(tmp_path, mode):
    f = fixture(tmp_path, mode=mode)
    history = History()
    await invoke(f.h.groups, history, agent="agent")
    f.h.runtime.inspect_group_membership.return_value = {"status": "left"}
    assert await f.poller.poll_memberships_once() == 1
    assert not f.h.groups.active_accesses("local", "agent")
    await invoke(f.h.groups, history, agent="agent")
    payload = directory_payload(history.messages[-1])
    assert payload["directory"]["groups"] == []
    assert payload["changes"] == [{"event": "已移出", "group_id": f.groups[0].group_chat_id,
                                    "name": f.groups[0].group_chat_id}]
    f.clock[0] += timedelta(minutes=1)
    assert await f.poller.poll_memberships_once() == 0
    await invoke(f.h.groups, history, agent="agent")
    assert len(history.messages) == 2
    f.h.inbound._sessions.resolve_session.assert_not_called()
    f.h.inbound._service.read_settings.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "exception"])
async def test_unavailable_probe_preserves_trusted_membership_and_retries(tmp_path, failure):
    f = fixture(tmp_path)
    before = f.h.groups.active_accesses("local", "agent")
    if failure == "exception":
        f.h.runtime.inspect_group_membership.side_effect = RuntimeError("private upstream response")
    else:
        f.h.runtime.inspect_group_membership.return_value = {"status": "unknown"}
    await f.poller.poll_memberships_once()
    assert f.h.groups.active_accesses("local", "agent") == before
    f.h.runtime.inspect_group_membership.side_effect = None
    f.h.runtime.inspect_group_membership.return_value = {"status": "active"}
    f.clock[0] += timedelta(minutes=1)
    await f.poller.poll_memberships_once()
    assert f.h.groups.directory("local", "agent")[0]["status"] == "active"


@pytest.mark.asyncio
async def test_unknown_binding_requires_fresh_message_and_cannot_be_restored_by_positive_probe(tmp_path):
    f = fixture(tmp_path)
    f.h.groups.update_membership("local", "agent", f.groups[0].group_chat_id, "app", status="unknown",
                                observed_at=f.clock[0], source_ref="event:uncertain")
    retired = f.h.groups.access_for("local", "agent", f.groups[0].group_chat_id)
    f.h.runtime.inspect_group_membership.return_value = {"status": "unknown"}
    await f.poller.poll_memberships_once()
    assert not f.h.groups.active_accesses("local", "agent")
    f.clock[0] += timedelta(minutes=1)
    f.h.runtime.inspect_group_membership.return_value = {"status": "active"}
    await f.poller.poll_memberships_once()
    assert f.h.groups.active_accesses("local", "agent") == ()
    assert f.h.groups.access_for("local", "agent", f.groups[0].group_chat_id) == retired
    history = History()
    await invoke(f.h.groups, history, agent="agent")
    assert directory_payload(history.messages[-1])["directory"]["groups"] == []
    for message_time in (NOW, retired.verified_at):
        f.h.groups.observe_access("local", "agent", f.groups[0].group_chat_id, "app",
                                  observed_at=f.clock[0], message_time=message_time,
                                  source_ref="dingtalk:message:replayed")
        assert f.h.groups.active_accesses("local", "agent") == ()
    f.h.groups.observe_access("local", "agent", f.groups[0].group_chat_id, "app",
                              observed_at=f.clock[0], message_time=f.clock[0],
                              source_ref="dingtalk:message:fresh")
    assert f.h.groups.access_for("local", "agent", f.groups[0].group_chat_id).status == "active"
    await invoke(f.h.groups, history, agent="agent")
    assert directory_payload(history.messages[-1])["directory"]["active_count"] == 1


@pytest.mark.asyncio
async def test_unchanged_probe_does_not_change_authorization_revision(tmp_path):
    f = fixture(tmp_path)
    before = f.h.groups.active_accesses("local", "agent")
    await f.poller.poll_memberships_once()
    assert f.h.groups.active_accesses("local", "agent") == before
    assert f.h.runtime.inspect_group_membership.await_args.args[0].app_id == "app"


@pytest.mark.asyncio
async def test_rebinding_during_probe_cannot_update_old_agent(tmp_path):
    f = fixture(tmp_path)

    async def rebind(*args, **kwargs):
        f.h.config["channels"]["dingtalk"]["apps"][0]["persistent_agent_id"] = "another-agent"
        return {"status": "left"}

    f.h.runtime.inspect_group_membership.side_effect = rebind
    await f.poller.poll_memberships_once()
    assert f.h.groups.active_accesses("local", "agent")


@pytest.mark.asyncio
async def test_dissolution_during_probe_wins_over_old_membership_result(tmp_path):
    f = fixture(tmp_path)

    async def dissolve(*args, **kwargs):
        f.h.groups.mark_dissolved("local", f.groups[0].group_chat_id, observed_at=f.clock[0], source_ref="event:disband")
        return {"status": "unknown"}

    f.h.runtime.inspect_group_membership.side_effect = dissolve
    await f.poller.poll_memberships_once()
    assert f.h.groups.directory("local", "agent")[0]["status"] == "dissolved"
    assert f.h.groups.access_for("local", "agent", f.groups[0].group_chat_id).status == "left"


@pytest.mark.asyncio
async def test_departed_dissolved_and_other_platform_groups_are_not_probed(tmp_path):
    f = fixture(tmp_path, count=2)
    f.h.groups.mark_dissolved("local", f.groups[0].group_chat_id, observed_at=f.clock[0], source_ref="event:disband")
    f.h.groups.update_membership("local", "agent", f.groups[1].group_chat_id, "app", status="left",
                                observed_at=f.clock[0], source_ref="event:removed")
    group = f.h.groups.resolve("local", "feishu", "cid-feishu", now=NOW)
    f.h.groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=NOW,
                              message_time=NOW, source_ref="feishu:message:first")
    assert await f.poller.poll_memberships_once() == 0
    f.h.runtime.inspect_group_membership.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_budget_rotates_without_starving_later_groups(tmp_path):
    f = fixture(tmp_path, count=12)
    assert await f.poller.poll_memberships_once() == 8
    assert await f.poller.poll_memberships_once() == 8
    calls = f.h.runtime.inspect_group_membership.await_args_list
    assert len({call.kwargs["conversation_id"] for call in calls}) == 12


@pytest.mark.asyncio
async def test_start_and_close_manage_membership_task_even_for_passive_mode(tmp_path):
    f = fixture(tmp_path)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    f.poller.poll_once = AsyncMock(return_value=0)
    f.h.runtime.inspect_group_membership.side_effect = block
    await f.poller.start()
    await f.poller.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        await asyncio.wait_for(f.poller.aclose(), 2)
    assert cancelled.is_set()
    assert f.h.runtime.inspect_group_membership.await_count == 1
    assert await f.poller.poll_memberships_once() == 0
