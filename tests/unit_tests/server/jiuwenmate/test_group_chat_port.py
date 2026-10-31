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


@pytest.mark.parametrize('history_first', [False, True])
def test_dingtalk_receipt_and_verified_bot_echo_become_one_record(records, history_first):
    group = records.groups.resolve('local', 'dingtalk', 'cid', now=NOW)
    receipt = message(1, message_id='process-key', sender_id='app', sender_name='employee',
                      sender_kind='agent', text='Good morning', received_at=NOW + timedelta(seconds=3), reply_to='human-1')
    echo = message(2, message_id='platform-id', sender_id='dingtalk-open:bot', sender_name='Bot nickname',
                   sender_kind='agent', text=receipt.text, received_at=NOW + timedelta(seconds=1))
    items = [(receipt, 'delivery:del-1'), (echo, 'dingtalk:message:platform-id')]
    for item, source in reversed(items) if history_first else items:
        records.archive.append('local', group.group_chat_id, item, source_ref=source)
    reconcile = records.archive.reconcile_dingtalk_deliveries
    assert reconcile('local', group.group_chat_id, {'app': 'dingtalk-open:bot'},
                     observed_since=NOW - timedelta(seconds=20), observed_through=NOW + timedelta(seconds=30)) == 1
    assert reconcile('local', group.group_chat_id, {'app': 'dingtalk-open:bot'},
                     observed_since=NOW - timedelta(seconds=20), observed_through=NOW + timedelta(seconds=30)) == 0
    records.archive.append('local', group.group_chat_id, receipt, source_ref='delivery:del-1')
    records.archive.append('local', group.group_chat_id, echo, source_ref='dingtalk:message:platform-id')
    page = records.archive.history_page('local', group.group_chat_id, cursor=None, limit=20,
                                       visible_from=NOW, reader_id='employee')
    assert len(page.messages) == 1
    merged, = page.messages
    assert (merged.message_id, merged.sender_id, merged.sender_name, merged.reply_to) == (
        'platform-id', 'app', 'employee', 'human-1')
    assert merged.received_at == echo.received_at


@pytest.mark.parametrize('case', ['other_bot', 'human', 'different_text', 'late', 'attachment', 'ambiguous_echo',
                                  'ambiguous_receipt', 'unknown_bot', 'feishu'])
def test_dingtalk_echo_reconciliation_preserves_distinct_or_ambiguous_messages(records, case):
    group = records.groups.resolve('local', 'feishu' if case == 'feishu' else 'dingtalk', 'cid', now=NOW)
    receipt = message(1, message_id='process-key', sender_id='app', sender_kind='agent', text='Repeated text')
    echo = message(2, message_id='platform-id', sender_id='dingtalk-open:bot', sender_kind='agent', text=receipt.text)
    if case == 'other_bot':
        echo = echo.model_copy(update={'sender_id': 'dingtalk-open:other'})
    elif case == 'human':
        echo = echo.model_copy(update={'sender_kind': 'human'})
    elif case == 'different_text':
        echo = echo.model_copy(update={'text': 'Different'})
    elif case == 'late':
        echo = echo.model_copy(update={'received_at': NOW + timedelta(minutes=2)})
    elif case == 'attachment':
        receipt = receipt.model_copy(update={'attachment_refs': ('outgoing:file',)})
    records.archive.append('local', group.group_chat_id, receipt, source_ref='delivery:del-1')
    records.archive.append('local', group.group_chat_id, echo, source_ref='dingtalk:message:platform-id')
    if case.startswith('ambiguous_'):
        duplicate = echo if case == 'ambiguous_echo' else receipt
        source = 'dingtalk:message:another' if case == 'ambiguous_echo' else 'delivery:del-2'
        records.archive.append('local', group.group_chat_id, duplicate.model_copy(update={'message_id': 'another'}),
                               source_ref=source)
    mapping = {} if case == 'unknown_bot' else {'app': 'dingtalk-open:bot'}
    assert records.archive.reconcile_dingtalk_deliveries(
        'local', group.group_chat_id, mapping, observed_since=NOW - timedelta(seconds=20),
        observed_through=NOW + timedelta(seconds=30)) == 0
    page = records.archive.recent('local', group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert len(page.messages) == (3 if case.startswith('ambiguous_') else 2)


@pytest.mark.parametrize('since,through', [(0, 30), (-20, 5)])
def test_dingtalk_echo_requires_complete_past_and_future_candidate_window(records, since, through):
    group = records.groups.resolve('local', 'dingtalk', 'cid', now=NOW)
    receipt = message(1, message_id='process', sender_id='app', sender_kind='agent', text='Same text')
    echo = message(2, message_id='echo', sender_id='dingtalk-open:bot', sender_kind='agent', text=receipt.text)
    records.archive.append('local', group.group_chat_id, receipt, source_ref='delivery:del')
    records.archive.append('local', group.group_chat_id, echo, source_ref='dingtalk:message:echo')
    assert records.archive.reconcile_dingtalk_deliveries(
        'local', group.group_chat_id, {'app': 'dingtalk-open:bot'},
        observed_since=NOW + timedelta(seconds=since), observed_through=NOW + timedelta(seconds=through),
    ) == 0


def test_dingtalk_unsettled_receipt_still_counts_as_ambiguous_candidate(records):
    group = records.groups.resolve('local', 'dingtalk', 'cid', now=NOW)
    for mid, source, sender, seconds in [
        ('receipt-a', 'delivery:a', 'app', 0), ('receipt-b', 'delivery:b', 'app', 15),
        ('echo', 'dingtalk:message:echo', 'dingtalk-open:bot', 8),
    ]:
        records.archive.append('local', group.group_chat_id, message(
            1, message_id=mid, sender_id=sender, sender_kind='agent', text='Same',
            received_at=NOW + timedelta(seconds=seconds)), source_ref=source)
    assert records.archive.reconcile_dingtalk_deliveries(
        'local', group.group_chat_id, {'app': 'dingtalk-open:bot'},
        observed_since=NOW - timedelta(seconds=20), observed_through=NOW + timedelta(seconds=20),
    ) == 0


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


def test_a_group_discards_messages_older_than_the_latest_hundred(records):
    for number in range(1, 106):
        append(records, number)
    page = records.archive.recent(
        "local", records.group.group_chat_id, cursor=None, limit=200, visible_from=NOW,
    )
    assert [item.message_id for item in page.messages] == [f"m{number}" for number in range(6, 106)]


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
@pytest.mark.parametrize('platform,has_messages,expected', [
    ('dingtalk', True, 'found'), ('dingtalk', False, 'incomplete'), ('feishu', True, 'incomplete'),
])
async def test_known_observations_do_not_claim_complete_or_empty_platform_history(records, platform, has_messages, expected):
    port_class = import_module(f'{PKG}.ports.group_chat').HostGroupChatPort
    group = records.groups.resolve('local', platform, 'known-chat', now=NOW)
    records.groups.observe_access('local', 'agent', group.group_chat_id, 'app', observed_at=NOW, source_ref='known-member')
    records.archive.mark_gap('local', group.group_chat_id)
    if has_messages:
        records.archive.append('local', group.group_chat_id, message(1), source_ref='known-message')
    execution = core.ExecutionContext(
        namespace_id='local', agent_id='agent', session_id='session', request_id='request', execution_id='exec',
        source_ref='event:1', config_version='v1', config_fingerprint='a' * 64, principal_ref='principal',
        startup_scope=frozenset({'chat'}), source_scope=frozenset({'chat'}), policy_revision='p1',
        capability_snapshot=frozenset({'construction'}),
    )
    session = SimpleNamespace(agent_id='agent', conversation=SimpleNamespace(kind='group', key=group.group_chat_id))

    async def verify(access, current_group):
        return 'active'

    port = port_class(records.groups, records.archive, SimpleNamespace(get_session=lambda *args: session),
                      namespace_id='local', agent_id='agent', verify_membership=verify)
    result = await port.recent_messages(execution)
    assert result.status == expected
    assert records.archive.recent('local', group.group_chat_id, cursor=None, limit=8, visible_from=NOW).status == 'incomplete'
    if result.status == 'found':
        assert result.messages[0].message_id == 'm1'
        assert 'incomplete' in result.reason


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
