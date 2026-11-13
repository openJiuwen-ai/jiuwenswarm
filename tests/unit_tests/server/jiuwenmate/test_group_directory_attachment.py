"""Group membership metadata reaches each model window only when it changes."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.group_directory import GroupDirectoryRail
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


class History:
    def __init__(self):
        self.messages = []

    def get_messages(self, *, with_history=False):
        return self.messages

    async def add_messages(self, messages):
        self.messages.extend(messages if isinstance(messages, list) else [messages])


def observed_message(repo, platform="feishu", chat="oc_one", name="筹备群", agent="owner", ns="local", app="app"):
    group = repo.resolve(ns, platform, chat, now=NOW)
    repo.set_name(ns, group.group_chat_id, name)
    repo.observe_access(ns, agent, group.group_chat_id, app, observed_at=NOW,
                        message_time=NOW, source_ref=f"{platform}:message:{chat}")
    return group


def directory_payload(message):
    content = message.content.split("<workswarm_group_directory>\n", 1)[1]
    return json.loads(content.split("\n</workswarm_group_directory>", 1)[0])


async def invoke(repo, history, *, session="session-a", language="cn", agent="owner", ns="local"):
    manager = PromptAttachmentManager()
    manager.language = language
    rail = GroupDirectoryRail(repo, namespace_id=ns, agent_id=agent, session_id=session, language=language)
    rail.init(SimpleNamespace(prompt_attachment_manager=manager))
    ctx = SimpleNamespace(context=history, session=SimpleNamespace(get_session_id=lambda: session))
    await rail.on_user_message(ctx)
    await manager.sync_to_context(history, session)
    await rail.before_model_call(ctx)
    await manager.sync_to_context(history, session)
    return rail, manager, ctx


@pytest.mark.asyncio
async def test_first_snapshot_lists_both_platforms_without_reading_messages(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    a = observed_message(repo)
    b = observed_message(repo, "dingtalk", "cid_two", "筹备群")
    observed_message(repo, chat="oc_hidden", agent="other", name="OTHER-SECRET")
    observed_message(repo, chat="oc_other_ns", ns="other", name="NAMESPACE-SECRET")
    history = History()
    await invoke(repo, history)
    assert len(history.messages) == 1
    content = history.messages[0].content
    assert a.group_chat_id in content and b.group_chat_id in content
    assert "feishu" in content and "dingtalk" in content
    assert '"active_count":2' in content
    assert "OTHER-SECRET" not in content and "NAMESPACE-SECRET" not in content
    assert "审批" in content


@pytest.mark.asyncio
async def test_unchanged_recreated_invocation_does_not_append_again(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = observed_message(repo)
    history = History()
    await invoke(repo, history)
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="active",
                           observed_at=NOW + timedelta(seconds=1), source_ref="event:verified")
    await invoke(repo, history)
    assert len(history.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("departure", ["left", "dissolved"])
async def test_rename_and_departure_have_one_explicit_removal_update(tmp_path, departure):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = observed_message(repo, name="旧群名")
    history = History()
    await invoke(repo, history)
    repo.set_name("local", group.group_chat_id, "新群名")
    await invoke(repo, history)
    assert len(history.messages) == 2
    assert "群名更改" in history.messages[-1].content
    assert "旧群名" in history.messages[-1].content and "新群名" in history.messages[-1].content
    await invoke(repo, history)
    assert len(history.messages) == 2
    if departure == "left":
        repo.update_membership("local", "owner", group.group_chat_id, "app", status="left",
                               observed_at=NOW + timedelta(seconds=2), source_ref="event:left")
    else:
        repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=2),
                            source_ref="event:closed")
    await invoke(repo, history)
    payload = directory_payload(history.messages[-1])
    assert payload["directory"]["groups"] == []
    assert payload["directory"]["active_count"] == 0
    assert payload["changes"] == [{"event": "已移出", "group_id": group.group_chat_id, "name": "新群名"}]
    assert len(history.messages) == 3
    await invoke(repo, history)
    assert len(history.messages) == 3
    repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=3), source_ref="event:closed")
    await invoke(repo, history)
    assert len(history.messages) == 3
    assert sum(change["event"] == "已移出" for message in history.messages
               for change in directory_payload(message)["changes"]) == 1


@pytest.mark.asyncio
async def test_each_session_and_replaced_window_gets_its_own_snapshot(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    observed_message(repo)
    first, second = History(), History()
    await invoke(repo, first)
    await invoke(repo, second, session="session-b")
    assert len(first.messages) == len(second.messages) == 1
    first.messages.clear()
    await invoke(repo, first)
    assert len(first.messages) == 1


@pytest.mark.asyncio
async def test_first_message_adds_group_and_unknown_removes_it_once(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    history = History()
    await invoke(repo, history)
    assert '"active_count":0' in history.messages[-1].content
    group = observed_message(repo)
    await invoke(repo, history)
    assert "新加入" in history.messages[-1].content
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="unknown",
                           observed_at=NOW + timedelta(seconds=1), source_ref="binding:retired")
    await invoke(repo, history)
    payload = directory_payload(history.messages[-1])
    assert payload["directory"]["groups"] == []
    assert payload["directory"]["active_count"] == 0
    assert payload["changes"] == [{"event": "已移出", "group_id": group.group_chat_id, "name": "筹备群"}]
    await invoke(repo, history)
    assert len(history.messages) == 3


def test_dissolution_permanently_retires_all_members_despite_newer_positive_facts(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = observed_message(repo)
    observed_message(repo, agent="other")
    repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=2), source_ref="event:closed")
    assert repo.active_accesses("local", "owner") == ()
    assert repo.active_accesses("local", "other") == ()
    assert repo.directory("local", "owner")[0]["status"] == "dissolved"
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="active",
                           observed_at=NOW + timedelta(seconds=3), source_ref="event:newer")
    for agent, app in (("owner", "app"), ("other", "app"), ("new-agent", "new-app")):
        repo.observe_access("local", agent, group.group_chat_id, app,
                            observed_at=NOW + timedelta(seconds=4), message_time=NOW + timedelta(seconds=4),
                            source_ref="feishu:message:newer")
        assert repo.active_accesses("local", agent) == ()
    repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=1), source_ref="event:late")
    assert repo.directory("local", "owner")[0]["status"] == "dissolved"
    assert repo.directory("local", "new-agent") == ()


def test_dissolution_reason_survives_later_bot_removed_event(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = observed_message(repo)
    repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=1), source_ref="event:closed")
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="left",
                           observed_at=NOW + timedelta(seconds=2), source_ref="event:bot-removed")
    assert repo.directory("local", "owner")[0]["status"] == "dissolved"


def test_dissolution_before_first_join_blocks_late_events_and_plain_messages(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = repo.resolve("local", "feishu", "oc_late", now=NOW)
    repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=2), source_ref="event:closed")
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="active",
                           observed_at=NOW, source_ref="event:late-join")
    repo.observe_access("local", "owner", group.group_chat_id, "app", observed_at=NOW + timedelta(seconds=3),
                        message_time=NOW + timedelta(seconds=3), source_ref="feishu:message:after-dissolution")
    assert repo.active_accesses("local", "owner") == ()
    assert repo.directory("local", "owner") == ()


@pytest.mark.parametrize("source", ["event:joined", "membership:positive-probe"])
def test_positive_membership_fact_does_not_register_a_group_before_its_first_message(tmp_path, source):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = repo.resolve("local", "feishu", "oc_silent", now=NOW)
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="active",
                           observed_at=NOW, source_ref=source)
    assert repo.active_accesses("local", "owner") == ()
    assert repo.directory("local", "owner") == ()
    repo.observe_access("local", "owner", group.group_chat_id, "app", observed_at=NOW + timedelta(seconds=1),
                        message_time=NOW + timedelta(seconds=1), source_ref="feishu:message:first")
    assert len(repo.active_accesses("local", "owner")) == 1


@pytest.mark.parametrize("status,source", [("unknown", "event:uncertain"), ("unknown", "binding:retired"),
                                         ("left", "event:left")])
def test_retired_access_requires_message_time_strictly_after_retirement(tmp_path, status, source):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = observed_message(repo)
    retirement = NOW + timedelta(seconds=1)
    repo.update_membership("local", "owner", group.group_chat_id, "app", status=status,
                           observed_at=retirement, source_ref=source)
    retired = repo.access_for("local", "owner", group.group_chat_id)
    repo.update_membership("local", "owner", group.group_chat_id, "app", status="active",
                           observed_at=NOW + timedelta(seconds=2), source_ref="event:joined")
    assert repo.access_for("local", "owner", group.group_chat_id) == retired
    for message_time in (None, NOW, retirement):
        repo.observe_access("local", "owner", group.group_chat_id, "app",
                            observed_at=NOW + timedelta(seconds=3), message_time=message_time,
                            source_ref="feishu:message:replayed")
        assert repo.access_for("local", "owner", group.group_chat_id) == retired
        assert repo.active_accesses("local", "owner") == ()
    repo.observe_access("local", "owner", group.group_chat_id, "app",
                        observed_at=NOW + timedelta(seconds=4), message_time=NOW + timedelta(seconds=4),
                        source_ref="feishu:message:fresh")
    assert repo.access_for("local", "owner", group.group_chat_id).status == "active"


@pytest.mark.asyncio
async def test_first_snapshot_excludes_left_dissolved_and_unknown_groups(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    active = observed_message(repo)
    for status in ("left", "unknown", "dissolved"):
        group = observed_message(repo, chat=f"oc_{status}", name=f"hidden-{status}")
        if status == "dissolved":
            repo.mark_dissolved("local", group.group_chat_id, observed_at=NOW + timedelta(seconds=1),
                                source_ref="event:closed")
        else:
            repo.update_membership("local", "owner", group.group_chat_id, "app", status=status,
                                   observed_at=NOW + timedelta(seconds=1), source_ref="event:retired")
    history = History()
    await invoke(repo, history)
    payload = directory_payload(history.messages[-1])
    assert payload["directory"]["groups"] == [
        {"group_id": active.group_chat_id, "platform": "feishu", "name": "筹备群", "status": "active"},
    ]
    assert payload["directory"]["active_count"] == payload["directory"]["recorded_count"] == 1
    assert payload["changes"] == []


@pytest.mark.asyncio
async def test_one_invocation_reads_directory_once_but_rehydrates_a_lost_attachment(tmp_path, monkeypatch):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    observed_message(repo)
    original = repo.directory
    calls = []

    def read(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(repo, "directory", read)
    history = History()
    rail, manager, ctx = await invoke(repo, history)
    await rail.before_model_call(ctx)
    await manager.sync_to_context(history, "session-a")
    assert len(calls) == 1 and len(history.messages) == 1
    history.messages.clear()
    await rail.before_model_call(ctx)
    await manager.sync_to_context(history, "session-a")
    assert len(history.messages) == 1


@pytest.mark.asyncio
async def test_read_failure_is_not_reported_as_no_groups(tmp_path, monkeypatch):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")

    def fail(*args):
        raise RuntimeError("SECRET database path")

    monkeypatch.setattr(repo, "directory", fail)
    history = History()
    await invoke(repo, history)
    assert "暂不可用" in history.messages[-1].content
    assert "SECRET" not in history.messages[-1].content
    assert '"active_count":0' not in history.messages[-1].content


def test_duplicate_bindings_do_not_inflate_count_and_ambiguity_is_not_active(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = observed_message(repo)
    observed_message(repo, app="app-two")
    rows = repo.directory("local", "owner")
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    repo.update_membership("local", "owner", group.group_chat_id, "app-two", status="unknown",
                           observed_at=NOW + timedelta(seconds=1), source_ref="binding:old")
    assert repo.directory("local", "owner")[0]["status"] == "active"


@pytest.mark.asyncio
async def test_rail_refuses_another_session(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    rail, manager, ctx = await invoke(repo, History())
    ctx.session = SimpleNamespace(get_session_id=lambda: "other-session")
    with pytest.raises(ValueError, match="Session mismatch"):
        await rail.before_model_call(ctx)
    assert await manager.collect_for_session("other-session") == []


@pytest.mark.asyncio
async def test_english_directory_is_bounded_and_does_not_claim_complete_platform_discovery(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    for i in range(100):
        observed_message(repo, chat=f"oc_{i}", name="x" * 500)
    history = History()
    await invoke(repo, history, language="en")
    text = history.messages[-1].content
    assert len(text) < 12000
    assert '"active_count":100' in text
    assert "omitted" in text and "recorded" in text
    await invoke(repo, history, language="en")
    assert len(history.messages) == 1


@pytest.mark.asyncio
async def test_truncated_directory_keeps_departure_notices_without_false_joins(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3")
    for i in range(30):
        observed_message(repo, chat=f"oc_{i}", name="x" * 500)
    history = History()
    await invoke(repo, history)
    visible_first = repo.directory("local", "owner")[0]
    repo.update_membership("local", "owner", visible_first["group_id"], "app", status="left",
                           observed_at=NOW + timedelta(seconds=1), source_ref="event:left")
    await invoke(repo, history)
    text = history.messages[-1].content
    payload = directory_payload(history.messages[-1])
    assert payload["changes"] == [
        {"event": "已移出", "group_id": visible_first["group_id"], "name": "x" * 500},
    ]
    assert all(row["status"] == "active" and row["group_id"] != visible_first["group_id"]
               for row in payload["directory"]["groups"])
    assert len(text) < 12000
    await invoke(repo, history)
    assert len(history.messages) == 2


@pytest.mark.asyncio
async def test_previously_omitted_old_departures_cannot_crowd_out_a_real_departure(tmp_path):
    ids = iter(f"grp_{i:03}" for i in range(60))
    repo = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: next(ids))
    for i in range(60):
        group = observed_message(repo, chat=f"oc_{i}", name="x" * 500)
        if i < 30:
            repo.update_membership("local", "owner", group.group_chat_id, "app", status="left",
                                   observed_at=NOW + timedelta(seconds=1), source_ref="event:old-left")
    history = History()
    await invoke(repo, history)
    repo.update_membership("local", "owner", "grp_030", "app", status="left",
                           observed_at=NOW + timedelta(seconds=2), source_ref="event:new-left")
    await invoke(repo, history)
    payload = directory_payload(history.messages[-1])
    assert payload["changes"] == [{"event": "已移出", "group_id": "grp_030", "name": "x" * 500}]
    assert payload["directory"]["active_count"] == 29
    assert payload["changes_complete"] is False
    assert all(row["status"] == "active" and row["group_id"] != "grp_030"
               for row in payload["directory"]["groups"])
    await invoke(repo, history)
    assert len(history.messages) == 2
