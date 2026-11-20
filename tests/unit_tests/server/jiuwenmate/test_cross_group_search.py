from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive, time_micros
from jiuwenswarm.server.jiuwenmate.conversation_search import ConversationSearch
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from openjiuwen.harness import persistent_agent as core

NOW = datetime(2026, 10, 17, 8, tzinfo=timezone.utc)


def test_active_accesses_are_scoped_sorted_and_keep_multiple_apps(tmp_path):
    ids = iter(("grp_z", "grp_a"))
    groups = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: next(ids))
    group_z = groups.resolve("ns-1", "feishu", "chat-z", now=NOW)
    group_a = groups.resolve("ns-1", "feishu", "chat-a", now=NOW)

    for group_id, app_id in (
        (group_z.group_chat_id, "app-z"),
        (group_a.group_chat_id, "app-m"),
        (group_a.group_chat_id, "app-a"),
    ):
        groups.observe_access("ns-1", "agent-1", group_id, app_id,
                              observed_at=NOW, source_ref=f"joined:{app_id}")
    groups.update_membership("ns-1", "agent-1", group_z.group_chat_id, "app-left", status="left",
                             observed_at=NOW, source_ref="left:1")
    groups.observe_access("ns-1", "agent-2", group_z.group_chat_id, "app-other",
                          observed_at=NOW, source_ref="joined:other")

    accesses = groups.active_accesses("ns-1", "agent-1")

    assert tuple((item.group_id, item.app_id, item.status) for item in accesses) == (
        ("grp_a", "app-a", "active"),
        ("grp_a", "app-m", "active"),
        ("grp_z", "app-z", "active"),
    )


def test_active_group_members_are_scoped_and_exclude_retired_bindings(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=iter(("grp_a", "grp_b")).__next__)
    first = groups.resolve("ns-1", "feishu", "chat-a", now=NOW)
    second = groups.resolve("ns-1", "feishu", "chat-b", now=NOW)
    groups.observe_access("ns-1", "agent-a", first.group_chat_id, "app-a", observed_at=NOW, source_ref="joined:a")
    groups.observe_access("ns-1", "agent-a", second.group_chat_id, "app-b", observed_at=NOW, source_ref="joined:b")
    groups.observe_access("ns-1", "agent-b", first.group_chat_id, "app-c", observed_at=NOW, source_ref="joined:c")
    groups.update_membership("ns-1", "agent-c", first.group_chat_id, "app-d", status="left",
                             observed_at=NOW, source_ref="left:d")

    accesses = groups.active_group_members("ns-1", first.group_chat_id)

    assert tuple((item.agent_id, item.app_id) for item in accesses) == (("agent-a", "app-a"), ("agent-b", "app-c"))


def test_binding_retired_unknown_access_is_not_an_unresolved_membership(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: "grp_a")
    group = groups.resolve("ns-1", "feishu", "chat-a", now=NOW)
    groups.update_membership("ns-1", "agent-1", group.group_chat_id, "app-old", status="unknown",
                             observed_at=NOW, source_ref="binding:retired")

    assert groups.unresolved_accesses("ns-1", "agent-1") == ()


def test_search_confirms_empty_scope_with_only_retired_binding_rows(cross_group_records):
    records = cross_group_records
    records.groups.update_membership("ns-1", "agent-a", records.first.group_chat_id, "app-old",
                                     status="unknown", observed_at=NOW, source_ref="binding:retired")
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(query="topic"))

    assert result.status == "empty" and result.query_complete is True and not result.hits


def cursor_repository_type():
    try:
        return import_module("jiuwenswarm.server.jiuwenmate.group_search_cursor_repository").GroupSearchCursorRepository
    except ModuleNotFoundError:
        pytest.fail("JM-21 opaque cursor repository is not implemented")


def test_search_cursor_is_scoped_persistent_and_expires(tmp_path):
    repository_type = cursor_repository_type()
    db_path = tmp_path / "agents.sqlite3"
    repository = repository_type(db_path)
    state = {"targets": [{"group_id": "grp_a", "upper_ingest_seq": 4}]}
    token = repository.issue("ns-1", "agent-1", "a" * 64, state,
                             now=NOW, expires_at=NOW + timedelta(minutes=15))

    reopened = repository_type(db_path)
    assert reopened.load(token, "ns-1", "agent-1", "a" * 64, now=NOW) == state
    assert reopened.load(token, "ns-2", "agent-1", "a" * 64, now=NOW) is None
    assert reopened.load(token, "ns-1", "agent-2", "a" * 64, now=NOW) is None
    assert reopened.load(token, "ns-1", "agent-1", "b" * 64, now=NOW) is None
    assert reopened.load(token, "ns-1", "agent-1", "a" * 64,
                         now=NOW + timedelta(minutes=15)) is None


def test_search_cursor_database_does_not_store_the_raw_token(tmp_path):
    repository_type = cursor_repository_type()
    db_path = tmp_path / "agents.sqlite3"
    repository = repository_type(db_path)
    token = repository.issue("ns-1", "agent-1", "a" * 64, {"targets": []},
                             now=NOW, expires_at=NOW + timedelta(minutes=15))

    assert token.encode() not in db_path.read_bytes()


@pytest.fixture
def searchable_group(tmp_path):
    groups = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: "grp_alpha")
    group = groups.resolve("ns-1", "feishu", "chat-alpha", now=NOW)
    groups.set_name("ns-1", group.group_chat_id, "研发群")
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    return groups, group, archive, ConversationSearch(archive)


def archived_message(message_id, received_at, *, sender_id="alice", text=""):
    return core.GroupMessage(message_id=message_id, sender_id=sender_id, sender_name=sender_id,
                             sender_kind="human", received_at=received_at, text=text)


def test_search_page_is_stable_literal_and_bounded_by_snapshot(searchable_group):
    _, group, archive, search = searchable_group
    at = NOW + timedelta(seconds=10)
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m2", at, text="STRASSE 100%_ literal"), source_ref="event:m2")
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m1", at, text="Straße 100%_ literal"), source_ref="event:m1")
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m3", at, text="Straße 100XY literal"), source_ref="event:m3")
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m4", at, sender_id="bob", text="Straße 100%_ literal"),
                   source_ref="event:m4")
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m-old", at - timedelta(seconds=1), text="Straße 100%_ literal"),
                   source_ref="event:m-old")
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m-newer", at + timedelta(seconds=1), text="Straße 100%_ literal"),
                   source_ref="event:m-newer")
    snapshot = search.snapshot_group("ns-1", group.group_chat_id)
    query = core.GroupMessageQuery(query="STRASSE 100%_", since=at, until=at, sender_id="alice", limit=1)

    first = search.search_group_page("ns-1", group.group_chat_id, query, visible_from=at,
                                     upper_ingest_seq=snapshot.upper_ingest_seq, after_key=None, limit=1)
    archive.append("ns-1", group.group_chat_id,
                   archived_message("m-late", at, text="Straße 100%_ literal"), source_ref="event:m-late")
    second = search.search_group_page(
        "ns-1", group.group_chat_id, query, visible_from=at, upper_ingest_seq=snapshot.upper_ingest_seq,
        after_key=(time_micros(at), group.group_chat_id, "m1"), limit=1,
    )

    assert tuple(hit.message.message_id for hit in first.hits) == ("m1",)
    assert first.has_more is True
    assert first.hits[0].group_name == "研发群"
    assert tuple(hit.message.message_id for hit in second.hits) == ("m2",)
    assert second.has_more is False


def test_search_snapshot_reports_missing_archive_gap_and_content_revision(searchable_group):
    _, group, archive, search = searchable_group

    with pytest.raises(PersistentAgentError):
        search.snapshot_group("ns-1", group.group_chat_id)

    message = archived_message("m1", NOW, text="a body")
    archive.append("ns-1", group.group_chat_id, message, source_ref="event:m1")
    original = search.snapshot_group("ns-1", group.group_chat_id)
    archive.recall("ns-1", group.group_chat_id, "m1", source_ref="recall:m1", changed_at=NOW)
    edited = search.snapshot_group("ns-1", group.group_chat_id)
    archive.mark_gap("ns-1", group.group_chat_id)
    partial = search.snapshot_group("ns-1", group.group_chat_id)

    assert original.upper_ingest_seq == 1
    assert edited.content_revision > original.content_revision
    assert partial.has_gap is True


def _execution(agent_id="agent-a", session_id="session-a"):
    return core.ExecutionContext(
        namespace_id="ns-1", agent_id=agent_id, session_id=session_id, request_id="request-a",
        execution_id="execution-a", source_ref="event:search", config_version="v1",
        config_fingerprint="a" * 64, principal_ref="user-a", startup_scope=frozenset({"chat"}),
        source_scope=frozenset({"chat"}), policy_revision="policy-1",
        capability_snapshot=frozenset({"construction"}),
    )


def _search_port(groups, archive, accesses, *, agent_id="agent-a", sessions=None, verifier=None, cursors=None,
                clock=None, search=None):
    from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort
    from jiuwenswarm.server.jiuwenmate.group_search_cursor_repository import GroupSearchCursorRepository

    class Sessions:
        def get_session(self, namespace_id, session_id):
            session_agent, conversation_kind = sessions or (agent_id, "web_direct")
            return SimpleNamespace(agent_id=session_agent, conversation=SimpleNamespace(kind=conversation_kind))

    async def verify(access, group):
        return "active" if verifier is None else await verifier(access, group)

    cursors = cursors or GroupSearchCursorRepository(groups.db_path)
    return HostGroupChatPort(
        groups, archive, Sessions(), namespace_id="ns-1", agent_id=agent_id, verify_membership=verify,
        search=search or ConversationSearch(archive), cursors=cursors, clock=clock or (lambda: NOW),
    )


@pytest.fixture
def cross_group_records(tmp_path):
    ids = iter(("grp-alpha", "grp-beta", "grp-hidden", "grp-outside"))
    groups = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: next(ids))
    first = groups.resolve("ns-1", "feishu", "chat-alpha", now=NOW)
    second = groups.resolve("ns-1", "feishu", "chat-beta", now=NOW)
    hidden = groups.resolve("ns-1", "feishu", "chat-hidden", now=NOW)
    outside = groups.resolve("ns-1", "feishu", "chat-outside", now=NOW)
    groups.set_name("ns-1", first.group_chat_id, "研发群")
    groups.set_name("ns-1", second.group_chat_id, "产品群")
    groups.set_name("ns-1", hidden.group_chat_id, "产品群")
    groups.set_name("ns-1", outside.group_chat_id, "产品群")
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    return SimpleNamespace(root=tmp_path, groups=groups, first=first, second=second, hidden=hidden, outside=outside,
                           archive=archive, search=ConversationSearch(archive), db=tmp_path / "agents.sqlite3")


def _grant(records, agent_id, group, app_id=None):
    records.groups.observe_access("ns-1", agent_id, group.group_chat_id, app_id or f"app-{agent_id}",
                                  observed_at=NOW, source_ref=f"joined:{agent_id}:{group.group_chat_id}")


def _append_to(records, group, message_id, text, at=NOW):
    records.archive.append("ns-1", group.group_chat_id,
                           archived_message(message_id, at, text=text), source_ref=f"event:{message_id}")


def test_search_by_current_name_merges_authorized_group_results_only(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _grant(records, "agent-a", records.second)
    _grant(records, "agent-b", records.hidden)
    _append_to(records, records.first, "a1", "版本计划")
    _append_to(records, records.second, "b1", "版本计划", NOW + timedelta(seconds=1))
    _append_to(records, records.hidden, "h1", "版本计划", NOW + timedelta(seconds=2))
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(query="版本计划"))

    assert result.status == "found" and result.query_complete is True
    assert [(hit.group_id, hit.message.message_id) for hit in result.hits] == [
        (records.second.group_chat_id, "b1"), (records.first.group_chat_id, "a1")
    ]


def test_search_resolves_exact_authorized_group_name_and_intersects_id_filter(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _grant(records, "agent-a", records.second)
    _grant(records, "agent-b", records.outside)
    records.groups.set_name("ns-1", records.outside.group_chat_id, "财务群")
    _append_to(records, records.second, "b1", "roadmap")
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(
        query="roadmap", group_ids=(records.second.group_chat_id,), group_names=("产品群",),
    ))
    mismatched = await_search(port, _execution(), core.GroupMessageQuery(
        query="roadmap", group_ids=(records.first.group_chat_id,), group_names=("产品群",),
    ))
    non_exact = await_search(port, _execution(), core.GroupMessageQuery(query="roadmap", group_names=("产品",)))
    unauthorized_id = await_search(port, _execution(), core.GroupMessageQuery(
        query="roadmap", group_ids=(records.outside.group_chat_id,),
    ))
    unauthorized_name = await_search(port, _execution(), core.GroupMessageQuery(query="roadmap", group_names=("财务群",)))

    assert result.status == "found" and result.hits[0].group_id == records.second.group_chat_id
    assert mismatched.status == "unavailable" and not mismatched.hits
    assert non_exact.status == "unavailable" and not non_exact.hits
    assert unauthorized_id.status == "unavailable" and records.outside.group_chat_id not in unauthorized_id.reason
    assert unauthorized_name.status == "unavailable" and records.outside.group_chat_id not in unauthorized_name.reason


def test_ambiguous_name_only_discloses_authorized_group_ids(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.second)
    _grant(records, "agent-a", records.hidden)
    _grant(records, "agent-b", records.outside)
    _append_to(records, records.second, "b1", "topic")
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(query="topic", group_names=("产品群",)))
    disambiguated = await_search(port, _execution(), core.GroupMessageQuery(
        query="topic", group_ids=(records.second.group_chat_id,), group_names=("产品群",),
    ))

    assert result.status == "unavailable" and not result.hits
    assert records.second.group_chat_id in result.reason
    assert records.hidden.group_chat_id in result.reason
    assert records.outside.group_chat_id not in result.reason
    assert disambiguated.status == "found" and disambiguated.hits[0].group_id == records.second.group_chat_id


def test_unknown_membership_fails_before_opening_any_archive(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    opened = []
    search = SimpleNamespace(snapshot_group=lambda *args: opened.append(args))

    async def unknown(access, group):
        return "unknown"

    port = _search_port(records.groups, records.archive, None, verifier=unknown, search=search)
    result = await_search(port, _execution(), core.GroupMessageQuery(query="topic"))

    assert result.status == "unavailable" and not result.hits
    assert opened == []


def test_unresolved_stored_membership_does_not_become_complete_empty(cross_group_records):
    records = cross_group_records
    records.groups.update_membership("ns-1", "agent-a", records.first.group_chat_id, "app-agent-a",
                                     status="unknown", observed_at=NOW, source_ref="membership:unknown")
    opened = []
    search = SimpleNamespace(snapshot_group=lambda *args: opened.append(args))
    port = _search_port(records.groups, records.archive, None, search=search)

    result = await_search(port, _execution(), core.GroupMessageQuery(query="topic"))

    assert result.status == "unavailable" and not result.hits and not result.query_complete
    assert opened == []


def test_unresolved_stored_membership_blocks_explicit_group_id(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    records.groups.update_membership("ns-1", "agent-a", records.first.group_chat_id, "app-agent-a",
                                     status="unknown", observed_at=NOW + timedelta(seconds=1),
                                     source_ref="membership:unknown")
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(
        query="topic", group_ids=(records.first.group_chat_id,),
    ))

    assert result.status == "unavailable" and not result.hits and not result.query_complete


def test_cursor_paginates_stably_and_is_invalidated_by_archive_revision(cross_group_records):
    from jiuwenswarm.server.jiuwenmate.group_search_cursor_repository import GroupSearchCursorRepository

    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _grant(records, "agent-a", records.second)
    at = NOW + timedelta(seconds=1)
    _append_to(records, records.first, "a1", "topic", at)
    _append_to(records, records.second, "b1", "topic", at)
    _append_to(records, records.second, "b2", "topic", at)
    port = _search_port(records.groups, records.archive, None,
                        cursors=GroupSearchCursorRepository(records.db))
    query = core.GroupMessageQuery(query="topic", limit=1)

    first = await_search(port, _execution(), query)
    records.archive.recall("ns-1", records.second.group_chat_id, "b2", source_ref="recall:b2",
                           changed_at=NOW + timedelta(seconds=2))
    following = await_search(port, _execution(), query.model_copy(update={"cursor": first.next_cursor}))

    assert first.status == "incomplete" and first.next_cursor is not None
    assert (first.hits[0].group_id, first.hits[0].message.message_id) == (records.first.group_chat_id, "a1")
    assert following.status == "incomplete" and not following.hits and following.next_cursor is None


def test_cursor_pages_do_not_repeat_and_cannot_be_replayed_by_another_agent(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _grant(records, "agent-b", records.second)
    for message_id in ("m1", "m2", "m3"):
        _append_to(records, records.first, message_id, "release notes")
    port_a = _search_port(records.groups, records.archive, None)
    port_b = _search_port(records.groups, records.archive, None, agent_id="agent-b")
    query = core.GroupMessageQuery(query="release", limit=1)

    first = await_search(port_a, _execution(), query)
    _append_to(records, records.first, "m4", "release notes", NOW + timedelta(seconds=2))
    second = await_search(port_a, _execution(), query.model_copy(update={"cursor": first.next_cursor}))
    third = await_search(port_a, _execution(), query.model_copy(update={"cursor": second.next_cursor}))
    replayed = await_search(port_b, _execution("agent-b"), query.model_copy(update={"cursor": first.next_cursor}))

    assert [item.hits[0].message.message_id for item in (first, second, third)] == ["m1", "m2", "m3"]
    assert first.status == second.status == "incomplete"
    assert third.status == "found" and third.query_complete is True and third.next_cursor is None
    assert replayed.status == "unavailable" and not replayed.hits


def test_search_from_a_group_session_can_target_another_authorized_group(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _grant(records, "agent-a", records.second)
    _append_to(records, records.second, "b1", "sprint decision")
    port = _search_port(records.groups, records.archive, None, sessions=("agent-a", "group"))

    result = await_search(port, _execution(), core.GroupMessageQuery(query="sprint", group_names=("产品群",)))

    assert result.status == "found" and result.hits[0].group_id == records.second.group_chat_id


def test_missing_name_is_not_fabricated_from_the_group_id(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    with records.groups._transaction() as conn:
        conn.execute("DELETE FROM group_details WHERE namespace_id=? AND group_chat_id=?",
                     ("ns-1", records.first.group_chat_id))
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(
        query="decision", group_names=(records.first.group_chat_id,),
    ))

    assert result.status == "unavailable" and records.first.group_chat_id not in result.reason


def test_multiple_active_apps_for_one_group_fail_closed(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first, "app-a")
    _grant(records, "agent-a", records.first, "app-b")
    opened = []
    search = SimpleNamespace(snapshot_group=lambda *args: opened.append(args))
    port = _search_port(records.groups, records.archive, None, search=search)

    result = await_search(port, _execution(), core.GroupMessageQuery(query="decision"))

    assert result.status == "unavailable" and not result.hits and opened == []


def test_missing_archive_or_known_gap_never_becomes_complete_empty(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    port = _search_port(records.groups, records.archive, None)

    missing = await_search(port, _execution(), core.GroupMessageQuery(query="not-present"))
    _append_to(records, records.first, "m1", "other")
    records.archive.mark_gap("ns-1", records.first.group_chat_id)
    gap = await_search(port, _execution(), core.GroupMessageQuery(query="not-present"))

    assert missing.status == "failed" and not missing.query_complete and not missing.next_cursor
    assert gap.status == "incomplete" and not gap.query_complete and not gap.next_cursor


def test_one_unreadable_archive_makes_other_group_hits_incomplete(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _grant(records, "agent-a", records.second)
    _append_to(records, records.first, "m1", "known hit")
    port = _search_port(records.groups, records.archive, None)

    result = await_search(port, _execution(), core.GroupMessageQuery(query="known"))

    assert result.status == "incomplete" and not result.query_complete
    assert [(hit.group_id, hit.message.message_id) for hit in result.hits] == [(records.first.group_chat_id, "m1")]
    assert result.next_cursor is None


def test_membership_revoked_during_read_discards_hits(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _append_to(records, records.first, "m1", "sensitive decision")
    delegate = ConversationSearch(records.archive)

    class RevokingSearch:
        def snapshot_group(self, *args):
            return delegate.snapshot_group(*args)

        def search_group_page(self, namespace_id, group_id, query, **kwargs):
            page = delegate.search_group_page(namespace_id, group_id, query, **kwargs)
            records.groups.update_membership(
                namespace_id, "agent-a", group_id, "app-agent-a", status="left",
                observed_at=NOW + timedelta(seconds=1), source_ref="left:during-search",
            )
            return page

    port = _search_port(records.groups, records.archive, None, search=RevokingSearch())

    result = await_search(port, _execution(), core.GroupMessageQuery(query="sensitive"))

    assert result.status == "unavailable" and not result.hits


def test_cursor_reauthorizes_targets_before_opening_the_next_page(cross_group_records):
    records = cross_group_records
    _grant(records, "agent-a", records.first)
    _append_to(records, records.first, "m1", "topic")
    _append_to(records, records.first, "m2", "topic")
    port = _search_port(records.groups, records.archive, None)
    query = core.GroupMessageQuery(query="topic", limit=1)
    first = await_search(port, _execution(), query)
    opened = []

    class ReadSpy:
        def snapshot_group(self, *args):
            opened.append(args)
            return records.search.snapshot_group(*args)

        def search_group_page(self, *args, **kwargs):
            opened.append(args)
            return records.search.search_group_page(*args, **kwargs)

    port._search = ReadSpy()
    records.groups.update_membership("ns-1", "agent-a", records.first.group_chat_id, "app-agent-a",
                                     status="left", observed_at=NOW + timedelta(seconds=1), source_ref="left:page")
    next_page = await_search(port, _execution(), query.model_copy(update={"cursor": first.next_cursor}))

    assert next_page.status == "unavailable" and not next_page.hits
    assert opened == []


def await_search(port, execution, query):
    import asyncio

    return asyncio.run(port.search_messages(execution, query))
