# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive session scans must not hold the business event loop or multiply IO."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.recommendation import situation_report as report
from jiuwenswarm.common.background_work import BoundedBackgroundWork


@pytest.fixture
def report_reads(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation import calendar_source

    monkeypatch.setattr(report, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(report, "_read_future", None)
    monkeypatch.setattr(report, "_read_key", None)
    monkeypatch.setattr(report, "_local_reads", BoundedBackgroundWork(max_workers=1, name="test-report-read"))
    monkeypatch.setattr(report, "_LOCAL_READ_TIMEOUT", 5.0)
    monkeypatch.setattr(calendar_source, "fetch_calendar_events", AsyncMock(return_value=[]))
    return tmp_path


def test_only_top_active_histories_are_compressed(monkeypatch, report_reads):
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    metadata = {}
    for i in range(40):
        path = report_reads / str(i)
        path.mkdir()
        (path / "metadata.json").write_text("{}", encoding="utf-8")
        (path / "history.jsonl").write_text("", encoding="utf-8")
        metadata[str(i)] = {"mode": "agent.work.normal", "last_message_at": i + 1}
    metadata["39"]["mode"] = "team"
    metadata["38"]["last_message_at"] = 0
    monkeypatch.setattr(session_history, "get_read_history_path", lambda sid: report_reads / sid / "history.jsonl")
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda sid, enable_writeback: metadata[sid])
    compressed = []

    def compress(sid, max_rounds):
        compressed.append((sid, max_rounds))
        return "" if sid == "37" else sid

    monkeypatch.setattr(report, "_compress_history_for_profile", compress)
    result = report._scan_sessions(max_sessions=3, max_rounds=7)
    assert [s.session_id for s in result] == ["37", "36", "35"]
    assert compressed == [("37", 7), ("36", 7), ("35", 7)]
    assert result[0].compressed_history == "", "active delivery targets can have empty history"


@pytest.mark.asyncio
async def test_slow_history_stat_keeps_loop_alive_and_repeated_ticks_share_one_read(
    monkeypatch, report_reads,
):
    from jiuwenswarm.server.runtime.session import session_history, session_metadata
    from jiuwenswarm.agents.harness.common.recommendation import profile_extractor

    session = report_reads / "active"
    session.mkdir()
    (session / "metadata.json").write_text("{}", encoding="utf-8")
    (session / "history.jsonl").write_text("", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def history_path(sid):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return session / "history.jsonl"

    monkeypatch.setattr(session_history, "get_read_history_path", history_path)
    monkeypatch.setattr(session_metadata, "get_session_metadata",
                        lambda sid, enable_writeback: {"mode": "agent", "last_message_at": 1})
    monkeypatch.setattr(report, "_compress_history_for_profile", lambda sid, max_rounds: "history")
    monkeypatch.setattr(profile_extractor, "load_recommendation_state",
                        lambda: SimpleNamespace(recommendation_history=[]))
    monkeypatch.setattr(report, "_LOCAL_READ_TIMEOUT", 0.03)
    beats = []

    async def heartbeat():
        while True:
            beats.append(1)
            await asyncio.sleep(0.005)

    ticker = asyncio.create_task(heartbeat())
    first_tick = asyncio.create_task(report.build_situation_report())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first = await first_tick
        assert first.is_empty()
        beats_before = len(beats)
        others = await asyncio.gather(*(report.build_situation_report() for _ in range(20)))
        assert all(item.is_empty() for item in others)
        assert len(calls) == 1 and calls[0] != threading.get_ident()
        assert len(beats) > beats_before
        pending = report._read_future
    finally:
        release.set()
        ticker.cancel()
        await asyncio.gather(ticker, first_tick, return_exceptions=True)
    await asyncio.wrap_future(pending)
    monkeypatch.setattr(report, "_LOCAL_READ_TIMEOUT", 1)
    recovered = await report.build_situation_report()
    assert recovered.sessions[0].compressed_history == "history"


@pytest.mark.asyncio
async def test_cancelled_tick_does_not_cancel_read_or_admit_another_root(
    monkeypatch, report_reads,
):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read(max_rounds, mode_prefix):
        calls.append((max_rounds, mode_prefix))
        entered.set()
        assert release.wait(3)
        return report.SituationReport()

    monkeypatch.setattr(report, "_read_local_report", read)
    task = asyncio.create_task(report.build_situation_report())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending = report._read_future
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not pending.cancelled()
        assert (await report.build_situation_report(mode_prefix="code")).is_empty()
        assert len(calls) == 1
    finally:
        release.set()
    await asyncio.wrap_future(pending)


@pytest.mark.asyncio
async def test_repeated_timeouts_do_not_accumulate_completion_callbacks(monkeypatch, report_reads):
    release = threading.Event()

    def read(max_rounds, mode_prefix):
        assert release.wait(3)
        return report.SituationReport()

    monkeypatch.setattr(report, "_read_local_report", read)
    monkeypatch.setattr(report, "_LOCAL_READ_TIMEOUT", 0.001)
    try:
        await report.build_situation_report()
        pending = report._read_future
        original = pending.add_done_callback
        callbacks = []

        def add_callback(callback):
            callbacks.append(callback)
            original(callback)

        monkeypatch.setattr(pending, "add_done_callback", add_callback)
        await asyncio.gather(*(report.build_situation_report() for _ in range(100)))
        assert len(callbacks) <= 1, "every expired waiter retained another callback on hung IO"
    finally:
        release.set()
    await asyncio.to_thread(pending.result, 1)


@pytest.mark.asyncio
async def test_local_read_failure_skips_tick_then_recovers(monkeypatch, report_reads):
    calls = []

    def read(max_rounds, mode_prefix):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("local disk unavailable")
        return report.SituationReport(sessions=[report.SessionSummary("recovered", compressed_history="ok")])

    monkeypatch.setattr(report, "_read_local_report", read)
    assert (await report.build_situation_report()).is_empty()
    assert (await report.build_situation_report()).sessions[0].session_id == "recovered"


@pytest.mark.asyncio
async def test_thread_start_failure_skips_tick_then_recovers(monkeypatch, report_reads):
    def fail(*args, **kwargs):
        raise RuntimeError("cannot start another thread")

    monkeypatch.setattr(report, "_read_local_report",
                        lambda max_rounds, mode_prefix: report.SituationReport(
                            sessions=[report.SessionSummary("recovered")],
                        ))
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        assert (await report.build_situation_report()).is_empty()
    assert (await report.build_situation_report()).sessions[0].session_id == "recovered"


@pytest.mark.asyncio
async def test_shared_local_read_does_not_share_per_tick_skills(monkeypatch, report_reads):
    entered, release = threading.Event(), threading.Event()

    def read(max_rounds, mode_prefix):
        entered.set()
        assert release.wait(3)
        return report.SituationReport(sessions=[report.SessionSummary("shared")])

    monkeypatch.setattr(report, "_read_local_report", read)
    monkeypatch.setattr(report, "_LOCAL_READ_TIMEOUT", 1)
    first = asyncio.create_task(report.build_situation_report(skills=[{"name": "first"}]))
    second = asyncio.create_task(report.build_situation_report(skills=[{"name": "second"}]))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.01)
    finally:
        release.set()
    a, b = await asyncio.gather(first, second)
    assert a is not b
    assert "first" in a.skills_summary and "second" not in a.skills_summary
    assert "second" in b.skills_summary and "first" not in b.skills_summary
