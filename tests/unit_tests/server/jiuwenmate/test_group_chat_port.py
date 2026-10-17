"""JM-11: real per-group archives and host read boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from openjiuwen.harness import persistent_agent as core

NOW = datetime(2026, 10, 17, 8, tzinfo=timezone.utc)
PKG = "jiuwenswarm.server.jiuwenmate"


def archive_type():
    try:
        return import_module(f"{PKG}.conversation_archive").ConversationArchive
    except ModuleNotFoundError:
        pytest.fail("JM-11 archive is not implemented")


def message(number, **changes):
    values = dict(message_id=f"m{number}", sender_id="user", sender_name="Alice", sender_kind="human",
                  received_at=NOW + timedelta(seconds=number), text=f"决定 {number}")
    return core.GroupMessage(**(values | changes))


@pytest.fixture
def records(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3")
    group = groups.resolve("local", "feishu", "oc_group", now=NOW)
    archive = archive_type()(tmp_path / "group_messages", groups)
    return SimpleNamespace(root=tmp_path, groups=groups, group=group, archive=archive)


def append(records, number, **changes):
    return records.archive.append("local", records.group.group_chat_id, message(number, **changes),
                                  source_ref=f"feishu:message:m{number}")


def test_same_message_concurrent_receivers_and_restart_share_one_record(records):
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: append(records, 1), range(8)))
    assert len(set(refs)) == 1
    group_root = records.root / "group_messages" / records.group.group_chat_id
    assert (group_root / "messages.sqlite3").is_file() and (group_root / "attachments").is_dir()
    assert not (records.root / "group_messages" / "local").exists()
    reopened = archive_type()(records.root / "group_messages", records.groups)
    page = reopened.recent("local", records.group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert [item.message_id for item in page.messages] == ["m1"]


def test_recall_before_message_and_redelivery_never_restore_body(records):
    records.archive.recall("local", records.group.group_chat_id, "m1", source_ref="recall:1", changed_at=NOW)
    append(records, 1, attachment_refs=("feishu:attachment:m1:file",))
    append(records, 1)
    page = records.archive.recent("local", records.group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert page.messages[0].status == "recalled" and page.messages[0].text is None
    assert page.messages[0].attachment_refs == ()


def test_appending_a_recalled_message_cannot_keep_the_existing_body(records):
    append(records, 1)
    append(records, 1, status="recalled", text=None)
    append(records, 1)
    page = records.archive.recent("local", records.group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert page.messages[0].status == "recalled" and page.messages[0].text is None


def test_delta_pages_stop_at_fixed_watermark_and_unknown_cursor_is_not_empty(records):
    for number in (1, 2, 3, 4):
        append(records, number)
    first = records.archive.recent("local", records.group.group_chat_id, cursor="m1", limit=2, visible_from=NOW)
    assert [item.message_id for item in first.messages] == ["m2", "m3"]
    assert first.scanned_through == "m3" and first.next_cursor
    append(records, 5)
    second = records.archive.recent("local", records.group.group_chat_id, cursor=first.next_cursor,
                                    limit=2, visible_from=NOW)
    assert [item.message_id for item in second.messages] == ["m4"] and second.next_cursor is None
    unknown = records.archive.recent("local", records.group.group_chat_id, cursor="missing",
                                     limit=20, visible_from=NOW)
    assert unknown.status == "incomplete" and unknown.scanned_through is None


def test_read_missing_archive_does_not_create_database(records):
    with pytest.raises(PersistentAgentError):
        records.archive.recent("local", records.group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert not (records.root / "group_messages" / records.group.group_chat_id / "messages.sqlite3").exists()


def test_deleting_an_agent_removes_only_its_access_not_public_records(records):
    from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository

    group_id = records.group.group_chat_id
    for agent in ("agent-a", "agent-b"):
        records.groups.observe_access("local", agent, group_id, f"app-{agent}",
                                      observed_at=NOW, source_ref=f"joined:{agent}")
    append(records, 1)
    PersistentAgentRepository(records.root / "agents.sqlite3").delete("local", "agent-a")
    with pytest.raises(PersistentAgentError):
        records.groups.access_for("local", "agent-a", group_id)
    assert records.groups.access_for("local", "agent-b", group_id).status == "active"
    assert records.archive.recent("local", group_id, cursor=None, limit=20, visible_from=NOW).messages


def test_corrupt_archive_is_not_an_empty_record(records):
    append(records, 1)
    with records.archive.connection("local", records.group.group_chat_id, create=True) as conn:
        conn.execute("DROP TABLE messages")
    with pytest.raises(PersistentAgentError):
        records.archive.recent("local", records.group.group_chat_id, cursor=None, limit=20, visible_from=NOW)


def test_archive_refuses_cross_namespace_and_path_escape(records):
    with pytest.raises(PersistentAgentError):
        records.archive.append("other", records.group.group_chat_id, message(1), source_ref="msg:1")
    unsafe = GroupChatRepository(records.root / "unsafe.sqlite3", id_factory=lambda: "../outside")
    with pytest.raises(PersistentAgentError):
        unsafe.resolve("local", "feishu", "oc_bad", now=NOW)


def test_group_ids_cannot_collide_between_namespaces(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: "same")
    groups.resolve("a", "feishu", "oc_a", now=NOW)
    with pytest.raises(PersistentAgentError):
        groups.resolve("b", "feishu", "oc_b", now=NOW)


def test_membership_does_not_reactivate_from_late_message_and_rejoin_resets_visibility(records):
    groups, group_id = records.groups, records.group.group_chat_id
    groups.observe_access("local", "agent", group_id, "app", observed_at=NOW, source_ref="event:1")
    groups.update_membership("local", "agent", group_id, "app", status="left",
                             observed_at=NOW + timedelta(seconds=5), source_ref="left:1")
    groups.observe_access("local", "agent", group_id, "app", observed_at=NOW + timedelta(seconds=1),
                          source_ref="late:1")
    assert groups.access_for("local", "agent", group_id).status == "left"
    groups.update_membership("local", "agent", group_id, "app", status="active",
                             observed_at=NOW + timedelta(seconds=10), source_ref="joined:2")
    access = groups.access_for("local", "agent", group_id)
    assert access.visible_from == NOW + timedelta(seconds=10)


def test_literal_unicode_search_and_visibility_filter(records):
    try:
        search_class = import_module(f"{PKG}.conversation_search").ConversationSearch
    except ModuleNotFoundError:
        pytest.fail("JM-11 search is not implemented")
    append(records, 1, text="before")
    append(records, 2, text="Straße 100%_")
    append(records, 3, text="not a match")
    query = core.GroupMessageQuery(query="STRASSE 100%_")
    hits = search_class(records.archive).search_group("local", records.group.group_chat_id, query,
                                                     visible_from=NOW + timedelta(seconds=2))
    assert [item.message.message_id for item in hits] == ["m2"]


def test_cursor_is_bound_to_reader_and_invalidated_by_recall(records):
    for number in (1, 2, 3):
        append(records, number)
    page = records.archive.recent("local", records.group.group_chat_id, cursor="m1", limit=1,
                                  visible_from=NOW, reader_id="agent-a")
    wrong_reader = records.archive.recent("local", records.group.group_chat_id, cursor=page.next_cursor,
                                          limit=1, visible_from=NOW, reader_id="agent-b")
    assert wrong_reader.status == "incomplete" and not wrong_reader.messages
    records.archive.recall("local", records.group.group_chat_id, "m3", source_ref="recall:3", changed_at=NOW)
    stale = records.archive.recent("local", records.group.group_chat_id, cursor=page.next_cursor,
                                   limit=1, visible_from=NOW, reader_id="agent-a")
    assert stale.status == "incomplete" and not stale.messages


def test_refreshing_unchanged_messages_and_duplicate_recalls_preserves_cursor(records):
    for number in (1, 2, 3):
        append(records, number)
    records.archive.recall("local", records.group.group_chat_id, "m3", source_ref="recall:3", changed_at=NOW)
    page = records.archive.recent("local", records.group.group_chat_id, cursor="m1", limit=1, visible_from=NOW)
    records.archive.append("local", records.group.group_chat_id, message(2), source_ref="refresh:2", changed_at=NOW)
    records.archive.recall("local", records.group.group_chat_id, "m3", source_ref="recall:again", changed_at=NOW)
    following = records.archive.recent("local", records.group.group_chat_id, cursor=page.next_cursor,
                                       limit=1, visible_from=NOW)
    assert following.status == "found" and following.messages[0].message_id == "m3"
    assert following.messages[0].text is None


def test_membership_tie_cannot_restore_revoked_access(records):
    args = ("local", "agent", records.group.group_chat_id, "app")
    records.groups.update_membership(*args, status="left", observed_at=NOW, source_ref="left:1")
    records.groups.update_membership(*args, status="active", observed_at=NOW, source_ref="joined:late")
    assert records.groups.access_for(*args[:3]).status == "left"


def test_joining_after_binding_retirement_starts_a_new_visibility_period(records):
    args = ("local", "agent", records.group.group_chat_id, "app")
    records.groups.observe_access(*args, observed_at=NOW, source_ref="message:1")
    records.groups.update_membership(*args, status="unknown", observed_at=NOW + timedelta(seconds=1),
                                     source_ref="binding:retired")
    records.groups.update_membership(*args, status="active", observed_at=NOW + timedelta(seconds=2),
                                     source_ref="event:joined-again")
    assert records.groups.access_for(*args[:3]).visible_from == NOW + timedelta(seconds=2)


def test_edited_message_keeps_scan_identity_and_old_redelivery_does_not_undo_it(records):
    append(records, 1)
    records.archive.append("local", records.group.group_chat_id, message(1, text="new", status="edited"),
                           source_ref="edit:1", changed_at=NOW + timedelta(seconds=5))
    with pytest.raises(PersistentAgentError):
        append(records, 1)
    result = records.archive.recent("local", records.group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert result.messages[0].text == "new" and result.scanned_through == "m1"


@pytest.mark.asyncio
async def test_port_rechecks_membership_and_does_not_use_requester_group_membership(records):
    try:
        port_class = import_module(f"{PKG}.ports.group_chat").HostGroupChatPort
    except ModuleNotFoundError:
        pytest.fail("JM-11 group port is not implemented")
    append(records, 1)
    group_id = records.group.group_chat_id
    records.groups.observe_access("local", "agent", group_id, "app", observed_at=NOW, source_ref="joined:1")
    execution = core.ExecutionContext(
        namespace_id="local", agent_id="agent", session_id="session", request_id="request", execution_id="exec",
        source_ref="event:1", config_version="v1", config_fingerprint="a" * 64, principal_ref="not-a-member",
        startup_scope=frozenset({"chat"}), source_scope=frozenset({"chat"}), policy_revision="p1",
        capability_snapshot=frozenset({"construction"}),
    )
    session = SimpleNamespace(agent_id="agent", conversation=SimpleNamespace(kind="group", key=group_id))
    current = {"status": "active"}

    async def verify(access, group):
        return current["status"]

    port = port_class(records.groups, records.archive, SimpleNamespace(get_session=lambda *args: session),
                      namespace_id="local", agent_id="agent", verify_membership=verify)
    result = await port.recent_messages(execution)
    assert result.messages[0].message_id == "m1" and result.execution_id == "exec"

    async def recall_during_check(access, group):
        records.archive.recall("local", group_id, "m1", source_ref="recall:read", changed_at=NOW)
        return "active"

    guarded = port_class(records.groups, records.archive, SimpleNamespace(get_session=lambda *args: session),
                         namespace_id="local", agent_id="agent", verify_membership=recall_during_check)
    assert (await guarded.recent_messages(execution)).messages[0].text is None
    current["status"] = "left"
    with pytest.raises(PersistentAgentError):
        await port.recent_messages(execution)
    unavailable = await port.search_messages(execution, core.GroupMessageQuery(query="决定"))
    assert unavailable.status == "unavailable" and not unavailable.hits
    with pytest.raises(PersistentAgentError):
        await port.recent_messages(replace(execution, agent_id="other"))
