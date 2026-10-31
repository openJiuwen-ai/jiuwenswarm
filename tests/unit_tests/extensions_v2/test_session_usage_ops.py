# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Committed platform facts, shared Ops, and real user-package loading."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.common import utils
from jiuwenswarm.common.session_message import SESSION_MESSAGE_ORIGIN
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types import context_usage
from jiuwenswarm.extensions_v2.handler_ops.session_ops import session_turns, session_usage
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.runtime.session import session_statistics as stats
from .management.support import write_package


USER_CODE = '''
class Reader:
    def register(self, registrar):
        registrar.register_event_handler("test.statistics", self.handle, {})
        registrar.register_event_handler("context.usage.updated", self.handle, {})

    async def handle(self, event, ops, next_handler):
        usage = await ops.session.usage(breakdown="summary")
        compact = await ops.session.usage()
        turns = await ops.session.turns()
        if "tokens" in usage["context"]:
            usage["context"]["tokens"] = 999999
        again = await ops.session.usage(breakdown="summary")
        return {"turns": turns, "usage": again, "compact": compact,
                "bound_usage": ops.session_context.metadata.get("session_usage_info"),
                "cwd": await ops.session.cwd(), "root": await ops.session.root(),
                "model": await ops.session.model(), "ui": callable(ops.ui.render),
                "package": ops.package_metadata.package_id,
                "receipt": await next_handler(event)}

def create_extension():
    return Reader()
'''


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: root)
    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    yield root
    assert history.flush_pending_writes()


def _save(root, sid, records, *, legacy=False):
    directory = root / sid
    directory.mkdir(exist_ok=True)
    path = directory / ("history.json" if legacy else "history.jsonl")
    path.write_text(json.dumps(records) if legacy else "".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")
    return path


def _window(sid="s1", request="r1", *, estimated=False, tokens=120):
    return {
        "event_type": "context.usage", "session_id": sid, "request_id": request,
        "timestamp": "2026-10-28T01:00:00Z", "phase": "post_call", "sequence": 2,
        "context_window": {"input_tokens": tokens, "limit_tokens": 1000,
                           "occupancy_rate": tokens / 1000, "tokens_source": "provider_usage"},
        "parts": {"messages": {"category": "messages", "tokens": 90, "estimated": True}},
        "measurement": {"estimated": estimated, "authoritative_total": not estimated,
                        "total_source": "provider_usage" if not estimated else "string_length_fallback"},
    }


def _summary(request="r1", tokens=500):
    return {"role": "assistant", "event_type": "chat.usage_summary", "request_id": request,
            "timestamp": 12, "usage": {"input_tokens": tokens - 20, "output_tokens": 20,
                                      "total_tokens": tokens}}


def _ops(sid="s1", metadata=None):
    return SimpleNamespace(session_context=SessionContext(sid, metadata=metadata or {}))


@pytest.mark.parametrize("legacy", [False, True])
def test_turns_deduplicates_primary_inputs_excluding_other_activity(sessions, legacy):
    base = {"role": "user", "request_id": "r1", "content": "input"}
    rows = [base, dict(base), {**base, "request_id": "r2"},
            {"role": "assistant", "request_id": "model-retry"},
            {"role": "tool", "request_id": "tool-1"},
            {**base, "request_id": "child", "subagent_id": "sub-1"},
            {**base, "request_id": "relay", "message_origin": SESSION_MESSAGE_ORIGIN},
            {**base, "request_id": "extension", "extension_message_id": "note"},
            {**base, "request_id": "compact", "is_compact_summary": True},
            {**base, "request_id": "feedback", "feedback_only": True},
            {**base, "request_id": "cron", "channel_id": "cron"},
            {**base, "request_id": "tick", "metadata": {"automation": {"kind": "heartbeat"}}},
            {**base, "request_id": "system", "source": "proactive_recommendation"},
            {"role": "human", "id": "old-1"}, {"role": "human", "id": "old-1"},
            {"role": "user"}]
    _save(sessions, "s1", rows, legacy=legacy)
    assert stats.read_session_turns("s1") == 4


def test_empty_missing_and_deleted_do_not_invent_a_session(sessions):
    (sessions / "empty").mkdir()
    assert stats.read_session_turns("empty") == 0
    assert stats.read_session_usage("empty") is None
    assert stats.read_session_turns("unknown") is None
    assert stats.read_session_usage("unknown") is None
    assert not (sessions / "unknown").exists()


def test_reads_real_queued_inputs_and_survives_reload_compaction_and_truncation(sessions):
    for request in ("r1", "r1", "r2"):
        history.append_history_record(session_id="s1", request_id=request, channel_id="web",
                                      role="user", content="hello", timestamp=1)
    assert stats.read_session_turns("s1") == 2
    history.append_compact_history_records(session_id="s1", request_id="compact", channel_id="web",
                                          summary="short summary", timestamp=2)
    assert stats.read_session_turns("s1") == 2
    # A new process needs only the existing files, not a populated stats cache.
    assert len(json.loads(json.dumps(stats.read_session_usage("s1"))) or {}) == 0
    history.truncate_history_records(session_id="s1", cut_index=1)
    assert stats.read_session_turns("s1") == 1


@pytest.mark.parametrize("estimated", [False, True])
def test_usage_separates_window_from_turn_and_preserves_measurement(sessions, estimated):
    _save(sessions, "s1", [_window(estimated=estimated), _summary(tokens=500)])
    result = stats.read_session_usage("s1", True)
    assert result["source"] == "history"
    assert result["context"]["context_window"]["input_tokens"] == 120
    assert result["turn"]["total_tokens"] == 500
    assert result["turn"]["cache_tokens"] is None
    assert result["context"]["measurement"]["estimated"] is estimated
    assert result["context"]["parts"]["messages"]["tokens"] == 90
    assert "parts" not in stats.read_session_usage("s1")["context"]


def test_usage_latest_primary_window_ignores_child_member_and_payload_secrets(sessions):
    current = {**_window(request="r2"), "api_key": "secret-must-not-return", "content": "private text"}
    _save(sessions, "s1", [_window(), _summary(), current,
                           {**_window(request="child"), "depth": 1},
                           {**_window(request="member"), "member_name": "teammate"},
                           {**_window("foreign"), "request_id": "foreign"},
                           {**_summary("child"), "subagent_id": "sub-1"}])
    result = stats.read_session_usage("s1", True)
    assert result["context"]["request_id"] == "r2"
    assert result["turn"]["request_id"] == "r1"
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["context", "turn", "zero", "legacy-frame"])
def test_partial_missing_and_actual_zero_remain_distinct(sessions, kind):
    records = {"context": [_window()], "turn": [_summary()],
               "zero": [_window(tokens=0), {**_summary(), "usage": {"total_tokens": 0}}],
               "legacy-frame": [{"event_type": "context.usage", "rate": 0}]}[kind]
    _save(sessions, "s1", records)
    result = stats.read_session_usage("s1")
    if kind == "legacy-frame":
        assert result is None
    elif kind == "context":
        assert result["context"] and result["turn"] is None
    elif kind == "turn":
        assert result["turn"] and result["context"] is None
    else:
        assert result["context"]["context_window"]["input_tokens"] == 0
        assert result["turn"]["total_tokens"] == 0


@pytest.mark.parametrize("raw", ['{broken', '[]\n', '42\n'])
def test_corrupt_history_is_not_silent_zero(sessions, raw):
    path = _save(sessions, "s1", [])
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(stats.SessionStatisticsError):
        stats.read_session_turns("s1")


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "12"])
def test_invalid_usage_is_an_error(sessions, bad):
    _save(sessions, "s1", [{**_summary(), "usage": {"total_tokens": bad}}])
    with pytest.raises(stats.SessionStatisticsError):
        stats.read_session_usage("s1")


def test_write_queue_timeout_is_not_empty_success(sessions, monkeypatch):
    monkeypatch.setattr(history, "flush_pending_writes", lambda: False)
    with pytest.raises(stats.SessionStatisticsError, match="cannot read"):
        stats.read_session_turns("s1")
    monkeypatch.undo()


def test_io_failure_is_not_silent_zero(sessions, monkeypatch):
    def denied(session_id):
        raise PermissionError("unreadable")

    monkeypatch.setattr(stats, "read_committed_main_history", denied)
    with pytest.raises(stats.SessionStatisticsError):
        stats.read_session_turns("s1")


def test_deleted_session_and_statistics_reload_need_no_cache(sessions):
    path = _save(sessions, "s1", [_window(), _summary(), {"role": "user", "request_id": "r1"}])
    assert stats.read_session_turns("s1") == 1
    assert stats.read_session_usage("s1")["turn"]["total_tokens"] == 500
    path.unlink()
    path.parent.rmdir()
    assert stats.read_session_turns("s1") is None
    assert stats.read_session_usage("s1") is None
    assert not path.parent.exists()


@pytest.mark.asyncio
async def test_query_cancellation_has_no_business_write(sessions, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def wait_read(session_id):
        entered.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(stats, "read_committed_main_history", wait_read)
    task = asyncio.create_task(session_turns(_ops()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert not (sessions / "s1").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [session_turns, session_usage])
async def test_ops_reject_absent_or_invalid_context(method):
    with pytest.raises(RuntimeError):
        await method(SimpleNamespace(session_context=None))
    with pytest.raises(ValueError):
        await method(_ops("../outside"))


@pytest.mark.asyncio
async def test_host_snapshot_is_copied_and_not_replaced_by_later_history(sessions):
    snapshot = stats.build_usage_snapshot("s1", context_usage=_window(), turn_usage=_summary())
    ops = _ops(metadata={stats.SESSION_USAGE_INFO_KEY: snapshot})
    _save(sessions, "s1", [_window(tokens=300), _summary(tokens=800)])
    first = await session_usage(ops, True)
    first["context"]["context_window"]["input_tokens"] = 999
    assert (await session_usage(ops, True))["context"]["context_window"]["input_tokens"] == 120
    assert (await session_usage(_ops()))["turn"]["total_tokens"] == 800
    with pytest.raises(TypeError):
        await session_usage(ops, "true")
    with pytest.raises(ValueError):
        await session_usage(_ops("s2", {stats.SESSION_USAGE_INFO_KEY: snapshot}))


async def _core(event, ops, next_handler):
    return "core-receipt"


@pytest.fixture
async def host(sessions, tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "packages")
    instance = await assembly.install_extensions()
    instance.bus.register_event_type("test.statistics", lambda value, pattern: True, _core)
    package = write_package(tmp_path / "source", package_id="statistics.reader", module_source=USER_CODE)
    reports = await instance.manager.install([package])
    assert reports[0].status == "SUCCESS"
    try:
        yield instance
    finally:
        await assembly.shutdown_extensions()


@pytest.mark.asyncio
async def test_real_user_package_uses_shared_ops_on_other_event_and_isolates_sessions(host, sessions):
    for index in (1, 2):
        _save(sessions, f"s{index}", [{"role": "user", "request_id": f"r{x}"} for x in range(index)]
              + [_window(f"s{index}", tokens=100 * index), _summary(tokens=500 * index)])
    results = await asyncio.gather(*(
        host.bus.trigger(Event("test.statistics", {"session_id": "spoof"}), SessionContext(f"s{index}"))
        for index in (1, 2)
    ))
    assert [r["turns"] for r in results] == [1, 2]
    assert [r["usage"]["context"]["tokens"] for r in results] == [100, 200]
    assert [stats.read_session_usage(f"s{index}")["turn"]["total_tokens"] for index in (1, 2)] == [500, 1000]
    assert all(r["receipt"] == "core-receipt" and r["ui"] for r in results)
    assert all(r["cwd"] is None and r["root"] is None and r["model"] is None for r in results)
    assert all(r["package"] == "statistics.reader" for r in results)
    report = await host.manager.uninstall("statistics.reader")
    assert report.status == "SUCCESS"
    assert await host.bus.trigger(Event("test.statistics", {}), SessionContext("s1")) == "core-receipt"


@pytest.mark.asyncio
async def test_notify_binds_current_facts_before_summary_is_durable(host, sessions, monkeypatch):
    _save(sessions, "s1", [{"role": "user", "request_id": "r2"}, _summary("r1", 900)])
    seen = []
    original = host.bus.trigger

    async def record(event, context):
        result = await original(event, context)
        seen.append(result)
        return result

    monkeypatch.setattr(host.bus, "trigger", record)
    raw = _window(request="r2")
    task = context_usage.notify_context_usage(
        context_usage.ContextUsageUpdate("s1", "r2", input_tokens=20, total_tokens=20),
        context_usage=raw,
    )
    raw["context_window"]["input_tokens"] = 999
    await task
    result = seen[-1]
    assert result["turns"] == 1
    assert result["bound_usage"]["turn"]["total_tokens"] == 20
    assert result["bound_usage"]["turn"]["request_id"] == "r2"
    assert result["bound_usage"]["context"]["context_window"]["input_tokens"] == 120
    assert result["receipt"].accepted


async def _notify_query(host, monkeypatch, update, window):
    seen = []
    original = host.bus.trigger

    async def record(event, context):
        result = await original(event, context)
        seen.append(result)
        return result

    monkeypatch.setattr(host.bus, "trigger", record)
    await context_usage.notify_context_usage(update, context_usage=window)
    return seen[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing_request", "negative_tokens", "invalid_part"])
async def test_invalid_window_keeps_current_turn_instead_of_reading_old_history(host, sessions, monkeypatch, invalid):
    _save(sessions, "s1", [_summary("old-request", 900)])
    window = _window(request="current-request")
    if invalid == "missing_request":
        window.pop("request_id")
    elif invalid == "negative_tokens":
        window["context_window"]["input_tokens"] = -1
    else:
        window["parts"]["messages"]["category"] = "unsupported-category"
    result = await _notify_query(host, monkeypatch,
        context_usage.ContextUsageUpdate("s1", "current-request", input_tokens=20, total_tokens=20), window)
    assert result["bound_usage"]["source"] == "host"
    assert result["bound_usage"]["context"] is None
    assert result["bound_usage"]["turn"]["request_id"] == "current-request"
    assert result["bound_usage"]["turn"]["total_tokens"] == 20
    assert result["receipt"].accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_input", [-1, "invalid-input"])
async def test_invalid_turn_keeps_valid_context_snapshot(host, sessions, monkeypatch, invalid_input):
    _save(sessions, "s1", [_summary("old-request", 900)])
    result = await _notify_query(host, monkeypatch,
        context_usage.ContextUsageUpdate("s1", "current-request", input_tokens=invalid_input, total_tokens=20),
        _window(request="current-window"))
    assert result["bound_usage"]["source"] == "host" and result["bound_usage"]["turn"] is None
    assert result["bound_usage"]["context"]["request_id"] == "current-window"
    assert result["receipt"].accepted


@pytest.mark.asyncio
async def test_invalid_host_components_do_not_fall_back_to_a_previous_turn(host, sessions, monkeypatch):
    _save(sessions, "s1", [_summary("old-request", 900)])
    window = _window()
    window.pop("request_id")
    result = await _notify_query(host, monkeypatch,
        context_usage.ContextUsageUpdate("s1", "current-request", input_tokens=-1, total_tokens=-1), window)
    assert result["bound_usage"] is None
    assert result["usage"] == result["compact"] == {"context": {}, "rateLimits": []}
    assert result["receipt"].accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("input_tokens,cache_tokens,expected", [(0, 0, None), (10, 0, 0), (10, 7, 7)])
async def test_cache_presence_matches_current_notification_and_committed_summary(
    host, sessions, monkeypatch, input_tokens, cache_tokens, expected,
):
    _save(sessions, "s1", [_summary("old-request", 900)])
    update = context_usage.ContextUsageUpdate("s1", "current-request", input_tokens=input_tokens,
        output_tokens=20, total_tokens=input_tokens + 20, cache_tokens=cache_tokens)
    result = await _notify_query(host, monkeypatch, update, _window(request="current-window"))
    assert result["bound_usage"]["turn"]["cache_tokens"] == expected
    summary = {"input_tokens": input_tokens, "output_tokens": 20, "total_tokens": input_tokens + 20}
    if input_tokens > 0:
        summary["cache_tokens"] = cache_tokens
    _save(sessions, "s1", [{"role": "assistant", "event_type": "chat.usage_summary",
                            "request_id": "current-request", "timestamp": 12, "usage": summary}])
    restored = stats.read_session_usage("s1")
    assert restored["turn"]["cache_tokens"] == result["bound_usage"]["turn"]["cache_tokens"]


@pytest.mark.parametrize("marker", [
    {"depth": 1}, {"parent_invocation_id": "parent"}, {"member_name": "member"},
    {"team_id": "team"}, {"subagent_id": "child"}, {"mode": "subagent"},
])
def test_usage_history_ignores_non_primary_turn_summaries(sessions, marker):
    _save(sessions, "s1", [_window(), _summary("primary", 500), {**_summary("other", 999), **marker}])
    result = stats.read_session_usage("s1")
    assert result["turn"]["request_id"] == "primary" and result["turn"]["total_tokens"] == 500


def test_invalid_committed_context_remains_an_error(sessions):
    window = _window()
    window.pop("request_id")
    _save(sessions, "s1", [window, _summary("primary", 500)])
    with pytest.raises(stats.SessionStatisticsError, match="invalid committed"):
        stats.read_session_usage("s1")
