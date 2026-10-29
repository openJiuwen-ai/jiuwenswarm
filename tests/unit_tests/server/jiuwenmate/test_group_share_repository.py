"""Sharing receipts and grants use isolated SQLite files, never the owner's live store."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module, util
import json
import sqlite3

import pytest

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.search_contracts import GroupMessageSearchResult


NOW = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
SOURCES = [{"group_id": "source-a", "name": "研发群"}, {"group_id": "source-b", "name": "产品群"}]


def repository(tmp_path):
    module_name = "jiuwenswarm.server.jiuwenmate.group_share_repository"
    assert util.find_spec(module_name) is not None, "group sharing repository is not implemented"
    return import_module(module_name).GroupShareRepository(tmp_path / "agents.sqlite3")


def scope(**changes):
    value = {
        "namespace_id": "local", "config_id": "config-1", "agent_id": "agent-1", "request_id": "request-1",
        "principal_ref": "feishu:user:requester", "requester_name": "申请人",
        "target": {"channel": "feishu", "app_id": "app-1", "kind": "group", "id": "dest-1", "name": "接收群"},
        "query": {"query": "发布", "since": None, "until": None, "sender_id": None, "cursor": None,
                  "limit": 50, "group_ids": [], "group_names": []},
    }
    value.update(changes)
    return value


def result(bound_scope=None, *, texts=("机密甲", "机密乙")):
    bound_scope = bound_scope or scope()
    return GroupMessageSearchResult.model_validate({
        "namespace_id": bound_scope["namespace_id"], "agent_id": bound_scope["agent_id"],
        "session_id": "session-1", "request_id": bound_scope["request_id"],
        "execution_id": "execution-private", "source_ref": "source-private", "status": "found",
        "query_complete": True, "hits": [
            {"group_id": SOURCES[index % 2]["group_id"], "group_name": SOURCES[index % 2]["name"],
             "message_ref": f"private-ref-{index}",
             "message": {"message_id": f"message-{index}", "sender_id": "private-sender", "sender_name": "张三",
                         "sender_kind": "human", "received_at": NOW.isoformat(), "text": text,
                         "attachment_refs": ["private-attachment"]}}
            for index, text in enumerate(texts)
        ],
    }).model_dump(mode="json")


def create(repo, bound_scope=None, digest="a" * 64, frozen=None):
    bound_scope = bound_scope or scope()
    return repo.create_or_get(bound_scope, digest, frozen or result(bound_scope), deepcopy(SOURCES), NOW)


def decide(repo, row, decision="permanent", **changes):
    args = {"namespace_id": "local", "config_id": "config-1", "owner_principal": "local",
            "decision": decision, "now": NOW}
    args.update(changes)
    return repo.decide(row["approval_id"], **args)


def approve(repo, bound_scope=None, decision="permanent", digest="a" * 64):
    row = create(repo, bound_scope, digest)
    repo.bind_native("local", row["request_id"], "native-private")
    return decide(repo, row, decision)


def assert_no_content(value):
    encoded = json.dumps(value, ensure_ascii=False)
    for secret in ("机密甲", "机密乙", "private-ref-", "private-attachment", "private-sender"):
        assert secret not in encoded
    assert "result" not in value


def test_freezes_full_core_result_and_scope_across_reopen(tmp_path):
    repo = repository(tmp_path)
    original_scope, original_result = scope(), result()
    row = repo.create_or_get(original_scope, "a" * 64, original_result, deepcopy(SOURCES), NOW)
    assert_no_content(row)
    assert row["status"] == "pending" and row["native_approval_id"] is None
    original_result["hits"][0]["message"]["text"] = "mutated"
    original_scope["target"]["id"] = "mutated"
    reopened = repository(tmp_path)
    private = reopened.for_query("local", "agent-1", "request-1", "a" * 64)
    assert private["result"] == result()
    assert private["scope"] == scope()
    assert reopened.get(row["approval_id"]) == private
    private["result"]["hits"].clear()
    assert len(reopened.get(row["approval_id"])["result"]["hits"]) == 2
    assert reopened.get("absent") is None
    duplicate = create(reopened, frozen=result(texts=("new search content",)))
    assert duplicate["approval_id"] == row["approval_id"]
    assert reopened.get(row["approval_id"])["result"] == result()


@pytest.mark.parametrize("dimension", ["namespace_id", "agent_id", "request_id", "query_digest"])
def test_query_receipt_does_not_cross_identity_or_cursor_digest(tmp_path, dimension):
    repo = repository(tmp_path)
    create(repo)
    query = {"namespace_id": "local", "agent_id": "agent-1", "request_id": "request-1", "query_digest": "a" * 64}
    query[dimension] = "b" * 64 if dimension == "query_digest" else "other"
    assert repo.for_query(**query) is None
    if dimension == "query_digest":
        second = create(repo, scope(query={**scope()["query"], "cursor": "next-page"}), "b" * 64)
        assert second["status"] == "pending"
        assert len(repo.pending_for_request("local", "request-1")) == 2


@pytest.mark.parametrize("change", [
    {"config_id": "other"}, {"principal_ref": "feishu:attacker"},
    {"target": {**scope()["target"], "id": "other"}}, {"query": {"query": "different"}},
])
def test_duplicate_identity_cannot_rebind_scope(tmp_path, change):
    repo = repository(tmp_path)
    create(repo)
    with pytest.raises(PersistentAgentError) as exc:
        create(repo, scope(**change))
    assert exc.value.code == "CONFLICT"
    assert repo.for_query("local", "agent-1", "request-1", "a" * 64)["scope"] == scope()


def test_owner_view_previews_only_explicit_fields_and_requires_native_binding(tmp_path):
    repo = repository(tmp_path)
    row = create(repo)
    view, = repo.list_for_owner("local", "config-1")["approvals"]
    assert set(view) == {"approval_id", "request_id", "agent_id", "config_id", "status", "can_decide",
                         "requester_name", "source_groups", "target", "query", "summary", "messages",
                         "created_at", "expires_at", "decided_at"}
    assert view["messages"] == [
        {"group_id": "source-a", "group_name": "研发群", "message_id": "message-0", "sender_name": "张三", "text": "机密甲"},
        {"group_id": "source-b", "group_name": "产品群", "message_id": "message-1", "sender_name": "张三", "text": "机密乙"},
    ]
    assert view["query"] == scope()["query"]
    assert "2" in view["summary"] and not view["can_decide"]
    encoded = json.dumps(view)
    for secret in ("native-private", "private-ref-", "private-attachment", "private-sender", "execution-private"):
        assert secret not in encoded
    with pytest.raises(PersistentAgentError):
        decide(repo, row)
    repo.bind_native("local", "request-1", "native-private")
    assert repo.list_for_owner("local", "config-1")["approvals"][0]["can_decide"]
    assert repo.get(row["approval_id"])["native_approval_id"] == "native-private"
    assert repo.list_for_owner("other", "config-1") == {"approvals": [], "grants": []}
    assert repo.list_for_owner("local", "other") == {"approvals": [], "grants": []}


def test_owner_summary_describes_first_three_frozen_messages_only(tmp_path):
    repo = repository(tmp_path)
    snapshot = result(texts=("  发布计划\n下周上线  ", "风控审核通过后通知", "回滚预案已准备", "第四条不应进入摘要"))
    row = create(repo, frozen=snapshot)
    summary = repo.list_for_owner("local", "config-1")["approvals"][0]["summary"]
    for excerpt in ("发布计划 下周上线", "风控审核通过后通知", "回滚预案已准备"):
        assert excerpt in summary
    assert "第四条不应进入摘要" not in summary
    assert "接收群" in summary and "2 个来源群" in summary and "4 条消息" in summary
    assert "\n" not in summary and len(summary) <= 400
    for metadata in (row, *repo.pending_for_request("local", "request-1")):
        assert "summary" not in metadata
        assert "发布计划" not in json.dumps(metadata, ensure_ascii=False)
    assert repo.get(row["approval_id"])["result"] == snapshot
    assert repository(tmp_path).list_for_owner("local", "config-1")["approvals"][0]["summary"] == summary


def test_owner_summary_limits_each_excerpt_and_total_even_with_long_target_name(tmp_path):
    repo = repository(tmp_path)
    snapshot = result(texts=("甲" * 100 + "越界甲", "乙" * 100 + "越界乙", "丙" * 100 + "越界丙"))
    bound = scope(target={**scope()["target"], "name": "接收群" * 170})
    row = create(repo, bound, frozen=snapshot)
    view = repo.list_for_owner("local", "config-1")["approvals"][0]
    summary = view["summary"]
    for character in ("甲", "乙", "丙"):
        assert character * 99 in summary
        assert character * 100 not in summary
        assert "越界" + character not in summary
    assert len(summary) <= 400
    assert view["target"] == bound["target"]
    assert repo.get(row["approval_id"])["result"] == snapshot


@pytest.mark.parametrize("text", [None, "", " \n\t "])
def test_owner_summary_explicitly_marks_messages_without_body(tmp_path, text):
    repo = repository(tmp_path)
    create(repo, frozen=result(texts=(text,)))
    summary = repo.list_for_owner("local", "config-1")["approvals"][0]["summary"]
    assert "暂无正文" in summary
    assert len(summary) <= 400


def test_owner_summary_marks_empty_result_without_inventing_content(tmp_path):
    repo = repository(tmp_path)
    create(repo, frozen={**result(), "status": "empty", "hits": []})
    view = repo.list_for_owner("local", "config-1")["approvals"][0]
    assert "暂无正文" in view["summary"]
    assert view["messages"] == []


@pytest.mark.parametrize("legacy", [False, True])
def test_owner_view_normalizes_partial_query_without_rewriting_private_scope(tmp_path, legacy):
    partial_scope = scope(query={"query": "launch", "limit": 10, "group_ids": ["source-a"]})
    if legacy:
        legacy_database(tmp_path, "pending")
        with sqlite3.connect(tmp_path / "agents.sqlite3") as conn:
            conn.execute("UPDATE group_share_approvals SET scope_json=?", (json.dumps(partial_scope),))
        repo = repository(tmp_path)
        approval_id = "share-legacy-0"
    else:
        repo = repository(tmp_path)
        approval_id = create(repo, partial_scope)["approval_id"]
    views = repo.list_for_owner("local", "config-1")["approvals"]
    for view in views:
        assert view["query"] == {
            "query": "launch", "since": None, "until": None, "sender_id": None, "cursor": None,
            "limit": 10, "group_ids": ["source-a"], "group_names": [],
        }
    assert repo.get(approval_id)["scope"] == partial_scope


@pytest.mark.parametrize("owner", ["feishu:user:requester", "admin", "", None])
def test_nonlocal_principal_cannot_list_decide_or_revoke(tmp_path, owner):
    repo = repository(tmp_path)
    row = approve(repo)
    grant = repo.list_for_owner("local", "config-1")["grants"][0]
    for operation in (
        lambda: repo.list_for_owner("local", "config-1", owner),
        lambda: decide(repo, row, owner_principal=owner),
        lambda: repo.revoke(grant["grant_id"], namespace_id="local", config_id="config-1", owner_principal=owner, now=NOW),
    ):
        with pytest.raises(PersistentAgentError) as exc:
            operation()
        assert exc.value.code == "FORBIDDEN"


@pytest.mark.parametrize("boundary", [{"namespace_id": "other"}, {"config_id": "other"}])
def test_owner_mutations_check_namespace_and_config_even_on_replay(tmp_path, boundary):
    repo = repository(tmp_path)
    row = approve(repo)
    with pytest.raises(PersistentAgentError) as exc:
        decide(repo, row, **boundary)
    assert exc.value.code == "FORBIDDEN"
    grant = repo.list_for_owner("local", "config-1")["grants"][0]
    args = {"namespace_id": "local", "config_id": "config-1", "owner_principal": "local", "now": NOW}
    args.update(boundary)
    with pytest.raises(PersistentAgentError) as exc:
        repo.revoke(grant["grant_id"], **args)
    assert exc.value.code == "FORBIDDEN"
    assert repo.has_grant(scope(), "source-a")


@pytest.mark.parametrize("decision,status", [("once", "approved_once"), ("reject", "rejected"), ("permanent", "approved_permanent")])
def test_decisions_are_idempotent_conflicts_rejected_and_snapshot_immutable(tmp_path, decision, status):
    repo = repository(tmp_path)
    row = approve(repo, decision=decision)
    assert row["status"] == status
    assert decide(repo, row, decision) == row
    assert_no_content(row)
    with pytest.raises(PersistentAgentError) as exc:
        decide(repo, row, "reject" if decision != "reject" else "once")
    assert exc.value.code == "CONFLICT"
    assert repo.get(row["approval_id"])["result"] == result()
    assert not repo.pending_for_request("local", "request-1")
    approved = repo.approved_for_request("local", "request-1")
    assert len(approved) == (0 if decision == "reject" else 1)
    for item in approved:
        assert_no_content(item)
    assert not repo.approved_for_request("other", "request-1")
    assert repo.has_grant(scope(), "source-a") is (decision == "permanent")
    assert repo.has_grant(scope(), "source-b") is (decision == "permanent")


@pytest.mark.parametrize("dimension", ["namespace_id", "config_id", "agent_id", "channel", "app_id", "kind", "id"])
def test_permanent_grants_require_exact_source_and_destination(tmp_path, dimension):
    repo = repository(tmp_path)
    approve(repo)
    changed = scope()
    if dimension in changed["target"]:
        changed["target"][dimension] = "other"
    else:
        changed[dimension] = "other"
    assert not repo.has_grant(changed, "source-a")
    assert not repo.has_grant(scope(), "other-source")
    changed = scope(request_id="new-request")
    changed["target"]["name"] = "renamed"
    assert repo.has_grant(changed, "source-a")
    grants = repository(tmp_path).list_for_owner("local", "config-1")["grants"]
    assert {grant["source_group_id"] for grant in grants} == {"source-a", "source-b"}
    for grant in grants:
        assert set(grant) == {"grant_id", "source_group_id", "source_group_name", "target", "created_at", "status"}


def test_revocation_cannot_be_undone_by_old_receipts_or_grant_based_authorization(tmp_path):
    repo = repository(tmp_path)
    old = approve(repo)
    grants = repo.list_for_owner("local", "config-1")["grants"]
    grant = next(item for item in grants if item["source_group_id"] == "source-a")
    args = {"namespace_id": "local", "config_id": "config-1", "owner_principal": "local", "now": NOW}
    revoked = repo.revoke(grant["grant_id"], **args)
    assert revoked["status"] == "revoked"
    assert repo.revoke(grant["grant_id"], **args) == revoked
    assert decide(repo, old)["status"] == "approved_permanent"
    assert repo.authorize_from_grants(old["approval_id"], now=NOW)["status"] == "approved_permanent"
    assert not repo.has_grant(scope(), "source-a")
    next_row = create(repo, scope(request_id="request-2"))
    assert repo.authorize_from_grants(next_row["approval_id"], now=NOW)["status"] == "pending"
    assert repo.has_grant(scope(), "source-b")
    repo.bind_native("local", "request-2", "native-second")
    decide(repo, next_row)
    assert repo.has_grant(scope(), "source-a")
    assert repo.revoke(grant["grant_id"], **args)["status"] == "revoked"
    assert repo.has_grant(scope(), "source-a")
    grants = repo.list_for_owner("local", "config-1")["grants"]
    assert len([g for g in grants if g["source_group_id"] == "source-a" and g["status"] == "active"]) == 1


def test_authorize_from_grants_records_new_query_without_native_or_new_grants(tmp_path):
    repo = repository(tmp_path)
    approve(repo)
    grants = repo.list_for_owner("local", "config-1")["grants"]
    row = create(repo, scope(request_id="request-2"), "b" * 64)
    authorized = repo.authorize_from_grants(row["approval_id"], now=NOW)
    assert authorized["status"] == "approved_permanent" and authorized["native_approval_id"] is None
    assert_no_content(authorized)
    assert repo.approved_for_request("local", "request-2") == [authorized]
    assert repo.list_for_owner("local", "config-1")["grants"] == grants


@pytest.mark.parametrize("app_id", [None, ""])
def test_destinations_without_application_keep_their_exact_identity(tmp_path, app_id):
    repo = repository(tmp_path)
    bound = scope(target={**scope()["target"], "channel": "web", "kind": "direct", "app_id": app_id})
    approve(repo, bound)
    assert repo.has_grant(bound, "source-a")
    other = deepcopy(bound)
    other["target"]["app_id"] = "app-1"
    assert not repo.has_grant(other, "source-a")


def test_native_batch_binds_only_unbound_pending_rows_for_original_request(tmp_path):
    repo = repository(tmp_path)
    first = create(repo)
    second = create(repo, digest="b" * 64)
    other = create(repo, scope(namespace_id="other"))
    another_request = create(repo, scope(request_id="other"))
    repo.bind_native("local", "request-1", "native-batch")
    repo.bind_native("local", "request-1", "native-replacement")
    for row in (first, second):
        assert repo.get(row["approval_id"])["native_approval_id"] == "native-batch"
    for row in (other, another_request):
        assert repo.get(row["approval_id"])["native_approval_id"] is None
    found = repository(tmp_path).for_native("native-batch")
    assert found["approval_id"] in {first["approval_id"], second["approval_id"]}
    assert_no_content(found)
    assert repo.for_native("native-replacement") is None
    for row in (first, second):
        decide(repo, row, "once")
    assert repo.for_native("native-batch") is not None


def test_bind_call_preserves_first_tool_call_and_rejects_cross_request_or_native(tmp_path):
    repo = repository(tmp_path)
    first = create(repo)
    second = create(repo, digest="b" * 64)
    other = create(repo, scope(request_id="other-request"))
    assert repo.get(first["approval_id"])["tool_call_id"] is None
    repo.bind_call(first["approval_id"], "native-batch", "call-a")
    repo.bind_call(first["approval_id"], "native-batch", "call-a")
    repo.bind_call(second["approval_id"], "native-batch", "call-b")
    assert repository(tmp_path).get(first["approval_id"])["tool_call_id"] == "call-a"
    assert repository(tmp_path).get(second["approval_id"])["tool_call_id"] == "call-b"
    repo.bind_call(first["approval_id"], "native-batch", "call-a2")
    assert repo.get(first["approval_id"])["tool_call_ids"] == ["call-a", "call-a2"]
    for args in ((other["approval_id"], "native-batch", "call-c"),
                 (second["approval_id"], "other-native", "call-b")):
        with pytest.raises(PersistentAgentError) as exc:
            repo.bind_call(*args)
        assert exc.value.code == "CONFLICT"
    decide(repo, first, "once")
    repo.bind_call(first["approval_id"], "native-batch", "late-call")
    assert repo.get(first["approval_id"])["tool_call_ids"] == ["call-a", "call-a2", "late-call"]
    owner = repo.list_for_owner("local", "config-1")["approvals"]
    assert "tool_call_id" not in owner[0]
    assert "tool_call_ids" not in owner[0]


def test_bind_call_can_complete_a_request_level_native_binding(tmp_path):
    repo = repository(tmp_path)
    row = create(repo)
    repo.bind_native("local", "request-1", "native-batch")
    repo.bind_call(row["approval_id"], "native-batch", "call-a")
    assert repo.get(row["approval_id"])["tool_call_id"] == "call-a"


def test_unbound_expired_snapshot_can_link_only_to_settle_its_original_native_batch(tmp_path):
    repo = repository(tmp_path)
    row = create(repo)
    repo.expire_pending(datetime.fromisoformat(row["expires_at"]))
    before = repo.get(row["approval_id"])
    repo.bind_call(row["approval_id"], "recovered-native", "call-a")
    repo.bind_call(row["approval_id"], "recovered-native", "call-b")
    saved = repo.get(row["approval_id"])
    assert saved["status"] == "expired" and saved["decision"] is None
    assert saved["result"] == before["result"] and saved["expires_at"] == before["expires_at"]
    assert saved["tool_call_ids"] == ["call-a", "call-b"]
    assert not repo.list_for_owner("local", "config-1")["approvals"][0]["can_decide"]
    assert repo.list_for_owner("local", "config-1")["grants"] == []
    with pytest.raises(PersistentAgentError):
        repo.bind_call(row["approval_id"], "another-native", "call-c")


@pytest.mark.parametrize("decision", ["once", "permanent", "reject", "expired"])
def test_restart_can_relink_exact_decided_call_before_native_resume(tmp_path, decision):
    repo = repository(tmp_path)
    row = create(repo)
    repo.bind_call(row["approval_id"], "native-batch", "call-a")
    if decision == "expired":
        repo.expire_pending(datetime.fromisoformat(row["expires_at"]))
    else:
        decide(repo, row, decision)
    saved = repo.get(row["approval_id"])
    grants = repo.list_for_owner("local", "config-1")["grants"]
    reopened = repository(tmp_path)
    reopened.bind_call(row["approval_id"], "native-batch", "call-a")
    assert reopened.get(row["approval_id"]) == saved
    assert reopened.list_for_owner("local", "config-1")["grants"] == grants
    for native_id, call_id in (("other-batch", "call-a"), ("other-batch", "other-call")):
        with pytest.raises(PersistentAgentError) as exc:
            reopened.bind_call(row["approval_id"], native_id, call_id)
        assert exc.value.code == "CONFLICT"


@pytest.mark.parametrize("decision", ["once", "permanent", "reject", "expired"])
def test_same_query_multiple_calls_share_one_decision_and_recover_after_restart(tmp_path, decision):
    repo = repository(tmp_path)
    first, second = create(repo), create(repo)
    assert first["approval_id"] == second["approval_id"]
    assert first["tool_call_ids"] == []
    repo.bind_call(first["approval_id"], "native-batch", "call-a")
    repo.bind_call(second["approval_id"], "native-batch", "call-b")
    if decision == "expired":
        repo.expire_pending(datetime.fromisoformat(first["expires_at"]))
    else:
        decide(repo, first, decision)
    saved = repo.get(first["approval_id"])
    grants = repo.list_for_owner("local", "config-1")["grants"]
    reopened = repository(tmp_path)
    for call_id in ("call-b", "call-a", "call-b"):
        reopened.bind_call(first["approval_id"], "native-batch", call_id)
    assert reopened.get(first["approval_id"]) == saved
    reopened.bind_call(first["approval_id"], "native-batch", "call-c")
    restored = reopened.get(first["approval_id"])
    assert restored["tool_call_id"] == "call-a"
    assert restored["tool_call_ids"] == ["call-a", "call-b", "call-c"]
    for key in ("status", "decision", "decided_at", "result", "scope"):
        assert restored[key] == saved[key]
    assert reopened.list_for_owner("local", "config-1")["grants"] == grants
    assert len(grants) == (2 if decision == "permanent" else 0)
    assert reopened.for_native("native-batch")["tool_call_ids"] == ["call-a", "call-b", "call-c"]
    if decision in ("once", "permanent"):
        approved, = reopened.approved_for_request("local", "request-1")
        assert approved["tool_call_ids"] == ["call-a", "call-b", "call-c"]
        assert_no_content(approved)
    for native_id in ("different-native",):
        with pytest.raises(PersistentAgentError) as exc:
            reopened.bind_call(first["approval_id"], native_id, "call-c")
        assert exc.value.code == "CONFLICT"


def test_concurrent_same_query_call_bindings_do_not_lose_ids_or_duplicate_grants(tmp_path):
    repo = repository(tmp_path)

    def bind(index):
        worker = repository(tmp_path)
        row = create(worker)
        worker.bind_call(row["approval_id"], "native-batch", f"call-{index % 6}")
        return row["approval_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(bind, range(18)))
    assert len(set(ids)) == 1
    saved = repository(tmp_path).get(ids[0])
    assert saved["tool_call_ids"] == ["call-0", "call-1", "call-2", "call-3", "call-4", "call-5"]
    assert saved["tool_call_id"] in saved["tool_call_ids"]
    assert len(repo.pending_for_request("local", "request-1")) == 1
    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions = list(executor.map(lambda _: decide(repository(tmp_path), saved), range(8)))
    assert all(row["tool_call_ids"] == saved["tool_call_ids"] for row in decisions)
    assert len(repo.list_for_owner("local", "config-1")["grants"]) == 2


def legacy_database(tmp_path, status):
    """Create the previously shipped schema with no multi-call column, in a temporary DB."""
    with sqlite3.connect(tmp_path / "agents.sqlite3") as conn:
        conn.execute("""
            CREATE TABLE group_share_approvals (
                approval_id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, config_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, request_id TEXT NOT NULL, query_digest TEXT NOT NULL,
                scope_json TEXT NOT NULL, result_json TEXT NOT NULL, source_groups_json TEXT NOT NULL,
                native_approval_id TEXT, tool_call_id TEXT, status TEXT NOT NULL, decision TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT,
                UNIQUE(namespace_id, agent_id, request_id, query_digest)
            )
        """)
        for index, call_id in enumerate(("call-legacy", None)):
            conn.execute(
                "INSERT INTO group_share_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"share-legacy-{index}", "local", "config-1", "agent-1", "request-1", str(index) * 64,
                 json.dumps(scope()), json.dumps(result()), json.dumps(SOURCES), "native-legacy", call_id,
                 status, "once" if status == "approved_once" else None, NOW.isoformat(),
                 (NOW + timedelta(minutes=15)).isoformat(), NOW.isoformat() if status != "pending" else None),
            )


@pytest.mark.parametrize("status", ["pending", "approved_once", "expired"])
def test_legacy_single_call_schema_migrates_without_losing_receipt_or_content(tmp_path, status):
    legacy_database(tmp_path, status)
    repo = repository(tmp_path)
    first = repo.get("share-legacy-0")
    assert first["tool_call_id"] == "call-legacy"
    assert first["tool_call_ids"] == ["call-legacy"]
    assert first["result"] == result() and first["scope"] == scope() and first["status"] == status
    assert repo.get("share-legacy-1")["tool_call_ids"] == []
    repo.bind_call(first["approval_id"], "native-legacy", "call-new")
    reopened = repository(tmp_path)
    restored = reopened.get(first["approval_id"])
    assert restored["tool_call_id"] == "call-legacy"
    assert restored["tool_call_ids"] == ["call-legacy", "call-new"]
    assert restored["result"] == result() and restored["status"] == status
    assert "tool_call_ids" not in reopened.list_for_owner("local", "config-1")["approvals"][0]


def test_concurrent_legacy_migration_and_bindings_are_atomic(tmp_path):
    legacy_database(tmp_path, "pending")

    def migrate_and_bind(index):
        repo = repository(tmp_path)
        repo.bind_call("share-legacy-0", "native-legacy", f"call-{index}")

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(migrate_and_bind, range(6)))
    restored = repository(tmp_path).get("share-legacy-0")
    assert restored["tool_call_id"] == "call-legacy"
    assert restored["tool_call_ids"] == ["call-0", "call-1", "call-2", "call-3", "call-4", "call-5", "call-legacy"]


@pytest.mark.parametrize("path", ["sweep", "decide", "grants"])
def test_expiry_survives_restart_is_idempotent_and_never_creates_grants(tmp_path, path):
    repo = repository(tmp_path)
    if path == "grants":
        approve(repo, scope(request_id="existing"))
    row = create(repo)
    repo.bind_native("local", "request-1", "native-expiring")
    expiry = datetime.fromisoformat(row["expires_at"])
    assert not repo.expire_pending(expiry - timedelta(microseconds=1))
    repo = repository(tmp_path)
    if path == "sweep":
        expired, = repo.expire_pending(expiry)
        assert_no_content(expired)
    elif path == "decide":
        assert decide(repo, row, now=expiry)["status"] == "expired"
    else:
        assert repo.authorize_from_grants(row["approval_id"], now=expiry)["status"] == "expired"
    assert repo.get(row["approval_id"])["status"] == "expired"
    assert not repo.expire_pending(expiry + timedelta(days=1))
    assert not repo.approved_for_request("local", "request-1")
    assert not repo.pending_for_request("local", "request-1")
    assert not repo.list_for_owner("local", "config-1")["approvals"][-1]["can_decide"]
    if path != "grants":
        assert not repo.has_grant(scope(), "source-a")


def test_concurrent_create_decide_and_revoke_use_durable_transactions(tmp_path):
    repo = repository(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda _: create(repository(tmp_path)), range(12)))
    assert len({row["approval_id"] for row in rows}) == 1
    repo.bind_native("local", "request-1", "native-batch")
    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions = list(executor.map(lambda _: decide(repository(tmp_path), rows[0]), range(12)))
    assert all(row["status"] == "approved_permanent" for row in decisions)
    grants = repo.list_for_owner("local", "config-1")["grants"]
    assert len(grants) == 2
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: repository(tmp_path).revoke(
            grants[0]["grant_id"], namespace_id="local", config_id="config-1", owner_principal="local", now=NOW), range(12)))
    assert not repo.has_grant(scope(), grants[0]["source_group_id"])


def test_conflicting_concurrent_decisions_have_exactly_one_winner(tmp_path):
    repo = repository(tmp_path)
    row = create(repo)
    repo.bind_native("local", "request-1", "native-batch")

    def attempt(decision):
        try:
            return decide(repository(tmp_path), row, decision)["status"]
        except PersistentAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ["once", "permanent"]))
    assert outcomes.count("CONFLICT") == 1
    status = repo.get(row["approval_id"])["status"]
    assert repo.has_grant(scope(), "source-a") is (status == "approved_permanent")


def test_non_owner_repository_apis_never_return_candidate_content(tmp_path):
    repo = repository(tmp_path)
    row = create(repo)
    repo.bind_native("local", "request-1", "native-private")
    for value in (row, *repo.pending_for_request("local", "request-1"), repo.for_native("native-private"),
                  repo.authorize_from_grants(row["approval_id"], now=NOW)):
        assert_no_content(value)
    decided = decide(repo, row)
    for value in (decided, *repo.approved_for_request("local", "request-1")):
        assert_no_content(value)


def test_oversized_utf8_preview_is_rejected_atomically_without_truncation(tmp_path):
    repo = repository(tmp_path)
    oversized = result(texts=("密" * 60000,) * 3)
    with pytest.raises(PersistentAgentError):
        create(repo, frozen=oversized)
    assert repo.list_for_owner("local", "config-1") == {"approvals": [], "grants": []}
    accepted = result(texts=("密" * 60000,) * 2)
    row = create(repo, frozen=accepted)
    assert repo.get(row["approval_id"])["result"] == accepted
    assert len(repo.list_for_owner("local", "config-1")["approvals"][0]["messages"]) == 2


@pytest.mark.parametrize("invalid", ["unlisted_group", "wrong_invocation", "empty_sources", "invalid_json", "naive_time"])
def test_invalid_snapshots_never_create_partial_rows(tmp_path, invalid):
    repo = repository(tmp_path)
    snapshot = result()
    groups = deepcopy(SOURCES)
    now = NOW
    if invalid == "unlisted_group":
        groups.pop()
    elif invalid == "wrong_invocation":
        snapshot["agent_id"] = "other"
    elif invalid == "empty_sources":
        groups = []
    elif invalid == "invalid_json":
        snapshot["hits"][0]["message"]["text"] = object()
    else:
        now = NOW.replace(tzinfo=None)
    with pytest.raises(PersistentAgentError):
        repo.create_or_get(scope(), "a" * 64, snapshot, groups, now)
    assert not repo.pending_for_request("local", "request-1")


def test_empty_candidates_still_require_source_scope_approval(tmp_path):
    repo = repository(tmp_path)
    snapshot = {**result(), "status": "empty", "hits": []}
    row = create(repo, frozen=snapshot)
    assert row["status"] == "pending"
    assert repo.list_for_owner("local", "config-1")["approvals"][0]["messages"] == []


def test_schema_coexists_with_unrelated_agents_tables(tmp_path):
    path = tmp_path / "agents.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated_agents (value TEXT)")
        conn.execute("INSERT INTO unrelated_agents VALUES ('preserved')")
    repo = repository(tmp_path)
    approve(repo)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM unrelated_agents").fetchone()[0] == "preserved"
    assert len(repo.list_for_owner("local", "config-1")["grants"]) == 2

