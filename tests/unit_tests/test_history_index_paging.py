# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Legacy paging must preserve its contract without loading the entire file."""
import json
from types import SimpleNamespace
import tracemalloc

import pytest

from jiuwenswarm.server.control import history_service as service
from jiuwenswarm.server.runtime.session import history_paging as paging
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: tmp_path)
    path = tmp_path / "indexed" / "history.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(history, "_read_history_jsonl", lambda *_args: pytest.fail("whole JSONL read"))
    monkeypatch.setattr(history, "_read_history", lambda *_args: pytest.fail("whole JSON read"))
    return path


def write_rows(path, rows):
    text = json.dumps(rows, ensure_ascii=False) if path.suffix == ".json" else "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows
    )
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def semantic_records():
    return [
        {"id": "call", "role": "assistant", "event_type": "chat.tool_call",
         "event_payload": {"tool_call": {"id": "read-1", "name": "read_file",
                                        "arguments": {"file_path": "example.txt"}}}},
        {"id": "result", "role": "assistant", "event_type": "chat.tool_result",
         "event_payload": {"tool_call_id": "read-1", "tool_name": "read_file",
                           "rendered_result": "original tool result"}},
        {"id": "usage", "role": "assistant", "event_type": "chat.usage_summary",
         "usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
         "session_usage": {"revision": 2, "total_tokens": 336}},
        {"id": "nested-usage", "role": "assistant", "event_type": "chat.usage_summary",
         "event_payload": {"usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168}}},
    ]


@pytest.mark.parametrize("source_format", ["json", "jsonl", "large_jsonl"])
def test_indexed_page_preserves_replay_payload_and_usage(stored, semantic_records, source_format):
    path = stored.with_suffix(".json" if source_format == "json" else ".jsonl")
    rows = [dict(row) for row in semantic_records]
    if source_format == "large_jsonl":
        rows[1]["prompt"] = "x" * (5 * 1024 * 1024)
    write_rows(path, rows)
    original = path.read_bytes()
    result = service.load_history_page("indexed", 1)
    assert result == {"messages": list(reversed(semantic_records)), "total_pages": 1, "page_idx": 1}
    assert path.read_bytes() == original


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_indexed_page_rejects_oversized_semantic_payload_instead_of_omitting_it(stored, source_format):
    path = stored.with_suffix("." + source_format)
    write_rows(path, [{"role": "assistant", "event_type": "chat.tool_result",
                      "event_payload": {"rendered_result": "x" * (5 * 1024 * 1024)}}])
    original = path.read_bytes()
    with pytest.raises(HistoryReplayError) as error:
        service.load_history_page("indexed", 1)
    assert error.value.code == "HISTORY_RESTORE_LIMIT"
    assert path.read_bytes() == original


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
@pytest.mark.parametrize("count", [0, 50, 51, 103])
@pytest.mark.parametrize("page_idx", [1, 2, 3])
def test_indexed_pages_preserve_order_and_exact_total(stored, source_format, count, page_idx):
    path = stored.with_suffix("." + source_format)
    rows = []
    for index in range(count):
        rows.extend([
            {"id": f"audit-{index}", "role": "assistant", "event_type": "llm_call_start", "prompt": "audit"},
            {"id": str(index), "role": "user", "content": "中文\u2028question"},
            {"role": "user", "content": "  "},
        ])
    write_rows(path, rows)
    result = service.load_history_page("indexed", page_idx)
    expected_pages = max(1, (count + 49) // 50)
    if page_idx > expected_pages:
        assert result is None
    else:
        assert result["total_pages"] == expected_pages
        assert result["page_idx"] == page_idx
        assert [row["id"] for row in result["messages"]] == [
            str(index) for index in list(reversed(range(count)))[(page_idx - 1) * 50:page_idx * 50]
        ]


def test_indexed_filter_media_and_full_final_before_wire_truncation(stored):
    original = "回答\U0001f642" * 50000
    write_rows(stored, [
        {"id": "hidden", "role": "user", "mode": "team", "channel_id": "im", "content": "hidden"},
        {"id": "media", "role": "user", "content": "", "media_items": [{"path": "image.png"}]},
        {"id": "answer", "role": "assistant", "event_type": "chat.final", "content": original},
    ])
    result = service.load_history_page("indexed", 1)
    assert [row["id"] for row in result["messages"]] == ["answer", "media"]
    assert result["messages"][0]["content"] == original


def test_indexed_page_skips_giant_audit_in_bounded_memory(stored):
    with stored.open("wb") as output:
        output.write(b'{"prompt":"')
        for _ in range(64):
            output.write(b"x" * 1024 * 1024)
        output.write(b'","role":"assistant","event_type":"llm_call_start"}\n')
        output.write(b'{"id":"real","role":"user","content":"preserved"}\n')
    size = stored.stat().st_size
    tracemalloc.start()
    try:
        result = service.load_history_page("indexed", 1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [row["id"] for row in result["messages"]] == ["real"]
    assert peak < 3 * 1024 * 1024
    assert stored.stat().st_size == size


def test_indexed_page_preserves_subagent_and_missing_history_contract(stored):
    assert service.load_history_page("missing", 1) is None
    assert service.load_history_page("indexed", 1, subagent_id="missing") == {
        "messages": [], "total_pages": 1, "page_idx": 1, "subagent_id": "missing",
    }
    path = stored.parent / "subagents" / "child" / "history.jsonl"
    path.parent.mkdir(parents=True)
    write_rows(path, [{"id": "child", "role": "user", "content": "child"}])
    result = service.load_history_page("indexed", 1, subagent_id="child")
    assert result["messages"][0]["id"] == "child" and result["subagent_id"] == "child"


def test_indexed_page_collapses_inline_files_and_tolerates_bad_jsonl(stored):
    content = '<file-content path="example.txt">private file body</file-content>'
    write_rows(stored, [{"role": "user", "content": content}])
    with stored.open("ab") as output:
        output.write(b"invalid JSON\n")
        output.write('{"role":"user","content":"中文"}'.encode())
    result = service.load_history_page("indexed", 1)
    assert [row["content"] for row in result["messages"]] == ["中文", "@example.txt"]


@pytest.mark.parametrize("bad_row", [b"invalid JSON\n", b'{"role":"user","content":"\xff"}\n'])
def test_indexed_page_skips_bad_json_and_utf8_without_losing_neighbors(stored, bad_row):
    write_rows(stored, [{"id": "old", "role": "user", "content": "old question"}])
    with stored.open("ab") as output:
        output.write(bad_row)
        output.write(b'{"id":"new","role":"user","content":"new question"}\n')
    original = stored.read_bytes()
    result = service.load_history_page("indexed", 1)
    assert [row["id"] for row in result["messages"]] == ["new", "old"]
    assert result["total_pages"] == 1
    assert stored.read_bytes() == original


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_indexed_filter_runs_after_inline_file_normalization(stored, source_format):
    path = stored.with_suffix("." + source_format)
    write_rows(path, [
        {"id": "empty", "role": "user", "content": '<file-content path="">body</file-content>'},
        {"id": "real", "role": "user", "content": "real question"},
    ])
    result = service.load_history_page("indexed", 1)
    assert [row["id"] for row in result["messages"]] == ["real"]


@pytest.mark.parametrize("failure", ["page_budget", "allocation"])
def test_indexed_page_failures_are_explicit_instead_of_empty_success(stored, monkeypatch, failure):
    write_rows(stored, [{"role": "user", "content": "kept"}])
    if failure == "page_budget":
        monkeypatch.setattr(paging, "_PAGE_BYTES", 1)
    else:
        def no_memory(*_args, **_kwargs):
            raise MemoryError()
        monkeypatch.setattr(paging, "json", SimpleNamespace(loads=no_memory, dumps=json.dumps))
    with pytest.raises(HistoryReplayError) as error:
        service.load_history_page("indexed", 1)
    assert error.value.code == "HISTORY_RESTORE_LIMIT"


def test_indexed_page_rejects_changed_file_identity(stored, monkeypatch):
    write_rows(stored, [{"role": "user", "content": "old"}])
    original_stat = type(stored).stat
    calls = 0

    def changed_stat(path, *args, **kwargs):
        nonlocal calls
        stat = original_stat(path, *args, **kwargs)
        if path == stored:
            calls += 1
            if calls == 2:
                return SimpleNamespace(st_ino=stat.st_ino + 1, st_dev=stat.st_dev, st_size=stat.st_size)
        return stat

    monkeypatch.setattr(type(stored), "stat", changed_stat)
    with pytest.raises(HistoryReplayError) as error:
        paging.read_indexed_history(
            stored, session_id="indexed", page_idx=1, page_size=50, is_restorable=lambda _record: True,
        )
    assert error.value.code == "HISTORY_RESTORE_CHANGED"


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
@pytest.mark.parametrize("change", ["replace", "truncate", "remove"])
def test_indexed_snapshot_rejects_changes_before_or_during_parsing(stored, source_format, change):
    path = stored.with_suffix("." + source_format)
    write_rows(path, [{"role": "user", "content": "old committed question"}])
    snapshot = path.stat()
    if change == "replace":
        replacement = path.with_name("replacement." + source_format)
        write_rows(replacement, [{"role": "user", "content": "different question" * 200}])
        replacement.replace(path)
    elif change == "truncate":
        path.write_bytes(b"[]" if source_format == "json" else b"")
    else:
        path.unlink()
    with pytest.raises(HistoryReplayError) as error:
        paging.read_indexed_history(
            path, session_id="indexed", page_idx=1, page_size=50,
            is_restorable=lambda _record: True, source_stat=snapshot,
        )
    assert error.value.code == "HISTORY_RESTORE_CHANGED"


def test_indexed_snapshot_classifies_replacement_that_interrupts_parsing(stored, monkeypatch):
    path = stored.with_suffix(".json")
    write_rows(path, [{"role": "user", "content": "committed"}])
    replacement = path.with_name("replacement.json")
    write_rows(replacement, [{"role": "user", "content": "different question" * 200}])
    original = paging.iter_history_rows

    def changed_before_parse(*args, **kwargs):
        replacement.replace(path)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(paging, "iter_history_rows", changed_before_parse)
    with pytest.raises(HistoryReplayError) as error:
        paging.read_indexed_history(
            path, session_id="indexed", page_idx=1, page_size=50, is_restorable=lambda _record: True,
        )
    assert error.value.code == "HISTORY_RESTORE_CHANGED"


@pytest.mark.asyncio
async def test_legacy_unary_page_returns_budget_error(stored, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.handlers.session import handle_history_get_page

    write_rows(stored, [{"role": "user", "content": "preserved"}])
    original = stored.read_bytes()
    monkeypatch.setattr(paging, "_PAGE_BYTES", 1)
    replies = []

    class Sink:
        async def send_response(self, response):
            replies.append(response)

    request = AgentRequest(
        request_id="budget", channel_id="web", params={"session_id": "indexed", "page_idx": 1},
    )
    await handle_history_get_page(SimpleNamespace(request=request, sink=Sink()))
    assert len(replies) == 1 and not replies[0].ok
    assert replies[0].payload["code"] == "HISTORY_RESTORE_LIMIT"
    assert stored.read_bytes() == original

def test_indexed_page_preserves_complete_giant_tool_result(stored):
    from pathlib import Path

    original = "legacy-tool-body " * 400000
    write_rows(stored, [{
        "id": "large-tool", "role": "assistant", "event_type": "chat.tool_result",
        "rendered_result": original,
    }])
    source_bytes = stored.stat().st_size
    result = service.load_history_page("indexed", 1)
    record = result["messages"][0]
    reference = record["payload_refs"]["rendered_result"]
    assert reference["media_type"] == "application/json"
    assert json.loads(Path(reference["path"]).read_bytes()) == original
    assert len(record["rendered_result"]) < 1024
    assert stored.stat().st_size == source_bytes
