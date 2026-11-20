# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Legacy migration pages close cancelled requests without splitting tool batches."""

import json

import pytest
from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage

from jiuwenswarm.server.runtime.session import context_migration as migration
from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError
from jiuwenswarm.server.runtime.session.history_stream import iter_history_rows
from tests.unit_tests.test_context_snapshots import stored as stored, user


def _call(request_id, call_id):
    return {
        "role": "assistant",
        "request_id": request_id,
        "event_type": "chat.tool_call",
        "tool_call": {"tool_call_id": call_id, "name": "read", "arguments": "{}"},
    }


def _result(request_id, call_id, content="result"):
    return {
        "role": "assistant",
        "request_id": request_id,
        "event_type": "chat.tool_result",
        "tool_call_id": call_id,
        "rendered_result": content,
    }


def _completion(request_id, status="cancelled"):
    return {
        "id": f"{request_id}:request_completed",
        "role": "assistant",
        "request_id": request_id,
        "event_type": "chat.request_completed",
        "feedback_only": True,
        "status": status,
        "timestamp": 1.0,
        "content": "",
    }


def _read_pages(path, session_id, *, before_request_id=None, covered=None):
    pending = {"offset": 0, "done": False, "had_compaction": False}
    pages = []
    iterator = iter_history_rows(path, session_id=session_id)
    try:
        while not pending["done"]:
            page = migration._next_page(
                iterator, pending, before_request_id, covered or set()
            )
            if page:
                pages.append(page)
    finally:
        iterator.close()
    return pages, pending


@pytest.mark.parametrize("suffix", [".jsonl", ".json"])
def test_cancelled_call_does_not_join_later_closed_groups(stored, suffix):
    orphan = [user("cancelled"), _call("cancelled", "orphan"), _completion("cancelled")]
    subsequent = []
    for index in range(60):
        rid = f"turn-{index}"
        subsequent.extend(
            [user(rid), _call(rid, rid), _result(rid, rid, "x" * (100 * 1024))]
        )
    rows = orphan + subsequent
    path = stored.path.with_suffix(suffix)
    if suffix == ".json":
        path.write_text(json.dumps(rows), encoding="utf-8")
    else:
        stored.write(rows)

    pages, pending = _read_pages(path, stored.sid)

    assert len(pages) > 1
    assert [record for page in pages for record in page] == [
        orphan[0],
        orphan[1],
        *subsequent,
    ]
    assert all(
        sum(len(json.dumps(record).encode("utf-8")) for record in page)
        <= migration._MAX_CONTEXT_BYTES
        for page in pages
    )
    assert path.read_bytes()[pending["offset"] :] == (
        b"]" if suffix == ".json" else b""
    )


def test_tiny_records_are_paged_before_the_hard_record_limit(stored):
    rows = [user(f"turn-{index}", "small") for index in range(2050)]
    stored.write(rows)

    pages, _ = _read_pages(stored.path, stored.sid)

    assert len(pages) > 1
    assert max(map(len, pages)) < 2048
    assert [record for page in pages for record in page] == rows


def test_record_paging_waits_for_a_parallel_tool_batch(stored):
    rows = [user(f"turn-{index}", "small") for index in range(1023)]
    batch = [
        _call("parallel", "first"),
        _call("parallel", "second"),
        user("supplement", "received during tools"),
        _result("parallel", "second"),
        _result("parallel", "first"),
    ]
    stored.write(rows + batch + [user("next")])

    pages, _ = _read_pages(stored.path, stored.sid)

    assert pages[0] == rows + batch
    assert pages[1] == [user("next")]


def test_cancellation_only_closes_its_own_request_even_with_reused_tool_ids(stored):
    stored.write(
        [
            _call("cancelled", "shared"),
            _call("active", "shared"),
            _completion("cancelled"),
            user("next"),
        ]
    )

    with pytest.raises(HistoryReplayError, match="unfinished tool group"):
        _read_pages(stored.path, stored.sid)


def test_cancellation_preserves_reasoning_and_completed_parallel_results(stored):
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        _build_context_messages_from_history,
    )

    rows = [
        user("cancelled"),
        {
            "role": "assistant",
            "event_type": "chat.reasoning",
            "content": "KEEP_REASONING",
        },
        _call("cancelled", "completed"),
        _call("cancelled", "orphan"),
        _result("cancelled", "completed", "KEEP_RESULT"),
        _completion("cancelled"),
        user("next"),
    ]
    stored.write(rows)
    original = stored.path.read_bytes()

    pages, _ = _read_pages(stored.path, stored.sid)
    messages, _ = _build_context_messages_from_history(pages[0])

    assert pages == [rows[:5], rows[6:]]
    assert messages[1].reasoning_content == "KEEP_REASONING"
    assert [call.id for call in messages[1].tool_calls] == ["completed"]
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "completed"
    assert messages[2].content == "KEEP_RESULT"
    assert stored.path.read_bytes() == original


@pytest.mark.parametrize(
    "completion",
    [
        _completion("other"),
        _completion(""),
        _completion("pending", "success"),
        _completion("pending", "waiting_user"),
        _completion("pending", "running"),
    ],
)
def test_unrelated_or_non_cancelled_completion_does_not_hide_unfinished_calls(
    stored, completion
):
    stored.write([_call("pending", "unfinished"), completion, user("next")])

    with pytest.raises(HistoryReplayError, match="unfinished tool group"):
        _read_pages(stored.path, stored.sid)


def test_result_from_another_request_does_not_close_a_tool_call(stored):
    stored.write([_call("active", "shared"), _result("other", "shared"), user("next")])

    with pytest.raises(HistoryReplayError, match="unfinished tool group"):
        _read_pages(stored.path, stored.sid)


@pytest.mark.parametrize("missing_request", ["call", "result", "both"])
def test_legacy_tool_pair_with_missing_request_metadata_still_closes(
    stored, missing_request
):
    call, result = _call("legacy", "tool"), _result("legacy", "tool")
    if missing_request in {"call", "both"}:
        call.pop("request_id")
    if missing_request in {"result", "both"}:
        result.pop("request_id")
    stored.write([call, result])

    pages, _ = _read_pages(stored.path, stored.sid)

    assert pages == [[call, result]]


@pytest.mark.parametrize("suffix", [".jsonl", ".json"])
@pytest.mark.parametrize("identity", ["call_record_id", "user_record", "completion_record_id", "both_record_ids"])
def test_cancelled_legacy_call_uses_available_request_identity(stored, suffix, identity):
    call = _call("legacy", "orphan")
    completion = _completion("legacy")
    if identity != "completion_record_id":
        call.pop("request_id")
    if identity in {"call_record_id", "both_record_ids"}:
        call["id"] = "legacy:assistant"
    if identity in {"completion_record_id", "both_record_ids"}:
        completion.pop("request_id")
    prefix = [user("legacy")] if identity == "user_record" else []
    rows = prefix + [call, completion] + [user(f"next-{index}", "small") for index in range(2050)]
    path = stored.path.with_suffix(suffix)
    if suffix == ".json":
        path.write_text(json.dumps(rows), encoding="utf-8")
    else:
        stored.write(rows)
    original = path.read_bytes()

    pages, _ = _read_pages(path, stored.sid)

    assert [record for page in pages for record in page] == prefix + [call] + rows[len(prefix) + 2:]
    assert path.read_bytes() == original


@pytest.mark.parametrize("case", ["other_request", "overlapping_requests", "unknown_user", "unscoped_call"])
def test_legacy_cancellation_never_guesses_another_request(stored, case):
    call = _call("active", "orphan")
    call.pop("request_id")
    if case == "other_request":
        rows = [user("active"), call, _completion("other")]
    elif case == "overlapping_requests":
        rows = [user("active"), user("other"), call, _completion("active")]
    elif case == "unknown_user":
        anonymous_user = user("unknown")
        anonymous_user.pop("request_id")
        rows = [anonymous_user, user("active"), call, _completion("active")]
    else:
        rows = [call, _completion("active")]
    stored.write(rows)
    original = stored.path.read_bytes()

    with pytest.raises(HistoryReplayError, match="unfinished tool group"):
        _read_pages(stored.path, stored.sid)

    assert stored.path.read_bytes() == original


def test_legacy_request_scope_survives_a_page_boundary(stored, monkeypatch):
    monkeypatch.setattr(migration, "_PAGE_AT_RECORDS", 2)
    call = _call("legacy", "orphan")
    call.pop("request_id")
    rows = [user("legacy"), {"role": "assistant", "request_id": "legacy",
                             "event_type": "chat.reasoning", "content": "keep reasoning"},
            call, _completion("legacy"), user("next")]
    stored.write(rows)

    pages, _ = _read_pages(stored.path, stored.sid)

    assert [record for page in pages for record in page] == rows[:3] + rows[4:]


def test_completed_requests_do_not_make_a_later_legacy_call_ambiguous(stored):
    call = _call("legacy", "orphan")
    call.pop("request_id")
    rows = [user("old"), _call("old", "done"), _result("old", "done"),
            _completion("old", "success"), user("legacy"), call, _completion("legacy")]
    stored.write(rows)

    pages, _ = _read_pages(stored.path, stored.sid)

    assert [record for page in pages for record in page] == rows[:3] + rows[4:6]


@pytest.mark.parametrize("budget", ["bytes", "records"])
def test_an_oversized_unclosed_group_still_fails(stored, budget):
    rows = [_call("pending", "tool")]
    if budget == "bytes":
        rows.extend(user("pending", "x" * (1024 * 1024)) for _ in range(4))
    else:
        rows.extend(user("pending", "small") for _ in range(2048))
    stored.write(rows + [_completion("pending")])

    with pytest.raises(HistoryReplayError) as error:
        _read_pages(stored.path, stored.sid)

    assert error.value.code == "HISTORY_RESTORE_LIMIT"


def test_completion_keeps_the_excluded_request_and_covered_prefix_out_of_the_page(
    stored,
):
    rows = [
        _call("covered", "ignored"),
        _call("cancelled", "orphan"),
        _completion("cancelled"),
        user("current", "EXCLUDED"),
        _call("current", "pending"),
    ]
    stored.write(rows)

    pages, pending = _read_pages(
        stored.path, stored.sid, before_request_id="current", covered={"covered"}
    )

    assert pages == [[rows[1]]]
    assert pending["offset"] == stored.path.read_bytes().index(
        json.dumps(rows[3]).encode("utf-8")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case, tool_group_count", [
    ("cancelled", 60), ("cancelled", 61), ("legacy_cancelled", 61), ("tiny_records", 0),
])
async def test_native_migration_installs_a_complete_checkpoint_and_preserves_audit(
    stored, monkeypatch, case, tool_group_count
):
    rows = [
        user("cancelled", "CANCELLED_INPUT"),
        _call("cancelled", "orphan"),
        _completion("cancelled"),
    ]
    if case == "legacy_cancelled":
        rows[1].pop("request_id")
    if case != "tiny_records":
        for index in range(tool_group_count):
            rid = f"turn-{index}"
            rows.extend(
                [user(rid), _call(rid, rid), _result(rid, rid, "x" * (100 * 1024))]
            )
    else:
        rows.extend(user(f"turn-{index}", "small") for index in range(2050))
    stored.write(rows + [user("current", "EXCLUDED_CURRENT")])
    original = stored.path.read_bytes()
    engine = ContextEngine()
    original_create = engine.create_context
    compressed_tool_ids = []

    async def create(**kwargs):
        context = await original_create(**kwargs)

        async def compress(**_kwargs):
            messages = context.get_messages()
            calls = {
                call.id
                for message in messages
                if isinstance(message, AssistantMessage)
                for call in message.tool_calls or ()
            }
            results = [
                message.tool_call_id
                for message in messages
                if isinstance(message, ToolMessage)
            ]
            assert calls == set(results)
            assert "orphan" not in calls
            compressed_tool_ids.extend(results)
            context.set_messages([UserMessage(content="MIGRATED_SUMMARY")])
            return "compressed"

        monkeypatch.setattr(context, "compress_context", compress)
        return context

    monkeypatch.setattr(engine, "create_context", create)

    assert await migration.migrate_history_context(
        context_engine=engine,
        session=stored.session,
        processors=[],
        before_request_id="current",
    )

    messages = engine.get_context(session_id=stored.sid).get_messages()
    if case != "tiny_records":
        retained_tool_ids = [message.tool_call_id for message in messages if isinstance(message, ToolMessage)]
        retained_call_ids = {call.id for message in messages if isinstance(message, AssistantMessage)
                             for call in message.tool_calls or ()}
        assert retained_call_ids == set(retained_tool_ids)
        assert sorted(compressed_tool_ids + retained_tool_ids) == sorted(
            f"turn-{index}" for index in range(tool_group_count)
        )
    else:
        assert [message.content for message in messages] == ["CANCELLED_INPUT"] + [
            "small"
        ] * 2050
    assert "EXCLUDED_CURRENT" not in str(messages)
    reference = stored.session.disk[snapshots.SNAPSHOT_KEY]
    assert snapshots.load_checkpoint_snapshot(stored.sid, reference) is not None
    assert stored.path.read_bytes() == original
