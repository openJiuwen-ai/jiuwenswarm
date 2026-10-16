"""JM-16 activity query: ignored work is omitted, settled usage is counted."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from jiuwenswarm.server.jiuwenmate.activity_query import ActivityQuery, assemble_activity
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
NS = "local"

_SCHEMA = """
CREATE TABLE colleagues (
    namespace_id TEXT NOT NULL, agent_id TEXT NOT NULL, settings_json TEXT NOT NULL,
    source_template_id TEXT, is_default INTEGER NOT NULL, revision INTEGER NOT NULL,
    published_revision INTEGER, current_version TEXT, next_version INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, agent_id)
);
CREATE TABLE sessions (
    namespace_id TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    kind TEXT NOT NULL, channel TEXT NOT NULL, conversation_key TEXT NOT NULL,
    next_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, session_id)
);
CREATE TABLE requests (
    namespace_id TEXT NOT NULL, request_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL, source_ref TEXT NOT NULL, seq INTEGER NOT NULL,
    status TEXT NOT NULL, outcome TEXT, record_json TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, request_id)
);
CREATE TABLE executions (
    namespace_id TEXT NOT NULL, execution_id TEXT NOT NULL, request_id TEXT NOT NULL,
    recovery_ref TEXT, started_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, execution_id)
);
CREATE TABLE budget_calls (
    namespace_id TEXT NOT NULL, model_call_id TEXT NOT NULL, account_kind TEXT NOT NULL,
    account_key TEXT NOT NULL, ref_json TEXT NOT NULL, status TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL, account_revision INTEGER NOT NULL, reason TEXT,
    usage_json TEXT, charged_tokens INTEGER, needs_reconciliation INTEGER NOT NULL DEFAULT 0,
    admitted_at TEXT NOT NULL, settled_at TEXT,
    PRIMARY KEY (namespace_id, model_call_id)
);
CREATE TABLE candidate_outputs (
    namespace_id TEXT NOT NULL, output_id TEXT NOT NULL, request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL, event_id TEXT NOT NULL, target_ref TEXT NOT NULL,
    share_mode TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, output_id)
);
CREATE TABLE result_deliveries (
    namespace_id TEXT NOT NULL, delivery_id TEXT NOT NULL, request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL, output_id TEXT NOT NULL, channel TEXT NOT NULL,
    target_id TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0, external_message_id TEXT, error_code TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, file_key TEXT,
    PRIMARY KEY (namespace_id, delivery_id)
);
CREATE TABLE group_chats (
    namespace_id TEXT NOT NULL, group_chat_id TEXT NOT NULL, platform TEXT NOT NULL,
    platform_chat_id TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, group_chat_id)
);
"""


def _store(tmp_path):
    db = tmp_path / "agents.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(_SCHEMA)
    return db


def _colleague(db, agent_id: str, created: str) -> None:
    _insert(
        db,
        "INSERT INTO colleagues VALUES (?, ?, '{}', NULL, 0, 1, 1, 'v1', 1, ?, ?)",
        (NS, agent_id, created, created),
    )


def _insert(db, sql: str, params: tuple) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(sql, params)
        conn.commit()


def test_ignored_request_is_not_a_task_reply_or_failure(tmp_path):
    db = _store(tmp_path)
    _colleague(db, "mate_a", "2026-10-01T00:00:00+00:00")
    _insert(
        db,
        "INSERT INTO sessions VALUES (?, 'sess_1', 'mate_a', 'web_direct', 'web', 'conv_1', 2, ?)",
        (NS, "2026-10-16T00:00:00+00:00"),
    )
    for seq, request_id, outcome, updated in (
        (1, "req_done", "completed", "2026-10-16T10:00:00+00:00"),
        (2, "req_skip", "ignored", "2026-10-16T11:00:00+00:00"),
        (3, "req_fail", "failed", "2026-10-16T11:30:00+00:00"),
    ):
        _insert(
            db,
            "INSERT INTO requests VALUES (?, ?, 'mate_a', 'sess_1', ?, ?, 'ended', ?, '{}', ?)",
            (NS, request_id, request_id, seq, outcome, updated),
        )
    for execution_id, request_id, started in (
        ("exe_done", "req_done", "2026-10-16T09:00:00+00:00"),
        ("exe_skip", "req_skip", "2026-10-16T10:30:00+00:00"),
        ("exe_fail", "req_fail", "2026-10-16T11:00:00+00:00"),
    ):
        _insert(db, "INSERT INTO executions VALUES (?, ?, ?, NULL, ?)", (NS, execution_id, request_id, started))
    _insert(
        db,
        "INSERT INTO budget_calls (namespace_id, model_call_id, account_kind, account_key, ref_json, status,"
        " reserved_tokens, account_revision, charged_tokens, needs_reconciliation, admitted_at, settled_at)"
        " VALUES (?, 'call_known', 'agent', 'mate_a', ?, 'settled', 10, 0, 25, 0, ?, ?)",
        (NS, json.dumps({"execution_id": "exe_done"}), "2026-10-16T09:00:00+00:00", "2026-10-16T09:01:00+00:00"),
    )
    _insert(
        db,
        "INSERT INTO budget_calls (namespace_id, model_call_id, account_kind, account_key, ref_json, status,"
        " reserved_tokens, account_revision, charged_tokens, needs_reconciliation, admitted_at, settled_at)"
        " VALUES (?, 'call_unknown', 'agent', 'mate_a', ?, 'settled', 40, 0, 40, 1, ?, ?)",
        (NS, json.dumps({"execution_id": "exe_fail"}), "2026-10-16T11:00:00+00:00", "2026-10-16T11:01:00+00:00"),
    )
    _insert(
        db,
        "INSERT INTO budget_calls (namespace_id, model_call_id, account_kind, account_key, ref_json, status,"
        " reserved_tokens, account_revision, charged_tokens, needs_reconciliation, admitted_at)"
        " VALUES (?, 'call_denied', 'agent', 'mate_a', ?, 'denied', 0, 0, NULL, 0, ?)",
        (NS, json.dumps({"execution_id": "exe_skip"}), "2026-10-16T10:30:00+00:00"),
    )

    report = ActivityQuery(db).report(NS, now=NOW)
    member = report["members"][0]
    assert member["tasks"] == 2
    assert member["replies"] == 1
    assert member["failures"] == 1
    assert member["calls"] == 2
    assert member["tokens"] == 65
    assert member["companion_days"] == 16
    assert member["days"] == [{"date": "2026-10-16", "tasks": 2, "minutes": 90, "calls": 2, "tokens": 65}]
    assert member["channels"] == [
        {"channel": "web", "calls": 2, "tokens": 65, "tasks": 2, "replies": 1, "failures": 1}
    ]
    assert "settings" not in member
    assert "persona" not in json.dumps(report)


def test_group_file_delivery_is_an_artifact_and_other_namespace_is_hidden(tmp_path):
    db = _store(tmp_path)
    _colleague(db, "mate_a", "2026-10-16T00:00:00+00:00")
    _insert(
        db,
        "INSERT INTO group_chats VALUES (?, 'grp_1', 'feishu', 'oc_room', ?)",
        (NS, "2026-10-16T00:00:00+00:00"),
    )
    _insert(
        db,
        "INSERT INTO sessions VALUES (?, 'sess_g', 'mate_a', 'group', 'group', 'grp_1', 1, ?)",
        (NS, "2026-10-16T00:00:00+00:00"),
    )
    _insert(
        db,
        "INSERT INTO requests VALUES (?, 'req_g', 'mate_a', 'sess_g', 'src', 1, 'ended', 'completed', '{}', ?)",
        (NS, "2026-10-16T08:30:00+00:00"),
    )
    _insert(db, "INSERT INTO executions VALUES (?, 'exe_g', 'req_g', NULL, ?)", (NS, "2026-10-16T08:00:00+00:00"))
    _insert(
        db,
        "INSERT INTO candidate_outputs VALUES (?, 'out_1', 'req_g', 'exe_g', 'evt_1', 'target', 'upload', '{}', ?)",
        (NS, "2026-10-16T08:20:00+00:00"),
    )
    _insert(
        db,
        "INSERT INTO result_deliveries (namespace_id, delivery_id, request_id, execution_id, output_id, channel,"
        " target_id, status, idempotency_key, attempt_count, created_at, updated_at)"
        " VALUES (?, 'del_1', 'req_g', 'exe_g', 'out_1', 'feishu', 'oc_room', 'delivered', 'key', 1, ?, ?)",
        (NS, "2026-10-16T08:20:00+00:00", "2026-10-16T08:21:00+00:00"),
    )
    _insert(
        db,
        "INSERT INTO colleagues VALUES ('other', 'mate_b', '{}', NULL, 0, 1, 1, 'v1', 1, ?, ?)",
        ("2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
    )

    member = ActivityQuery(db).report(NS, now=NOW)["members"][0]
    assert member["groups"] == [
        {
            "group_chat_id": "grp_1",
            "name": "oc_room",
            "platform": "feishu",
            "artifacts": 1,
            "tokens": 0,
            "tasks": 1,
            "last_response_at": "2026-10-16T08:30:00+00:00",
        }
    ]
    assert [item["agent_id"] for item in ActivityQuery(db).report("other", now=NOW)["members"]] == ["mate_b"]


def test_queued_request_without_execution_is_not_a_task():
    report = assemble_activity(
        colleagues=[{"agent_id": "mate_a", "created_at": "2026-10-16T00:00:00+00:00"}],
        requests=[
            {
                "request_id": "req_q",
                "agent_id": "mate_a",
                "session_id": "sess_1",
                "status": "queued",
                "outcome": None,
                "updated_at": "2026-10-16T01:00:00+00:00",
            }
        ],
        executions=[],
        sessions=[{"session_id": "sess_1", "agent_id": "mate_a", "kind": "web_direct", "channel": "web", "conversation_key": "c"}],
        calls=[],
        deliveries=[],
        groups=[],
        now=NOW,
    )
    assert report["members"][0]["tasks"] == 0
    assert report["members"][0]["replies"] == 0
    assert report["members"][0]["failures"] == 0


def test_missing_tables_are_not_reported_as_empty_activity(tmp_path):
    db = tmp_path / "empty.sqlite3"
    db.write_bytes(b"")
    with pytest.raises(PersistentAgentError) as raised:
        ActivityQuery(db).report(NS, now=NOW)
    assert raised.value.code == "ACTIVITY_UNAVAILABLE"
