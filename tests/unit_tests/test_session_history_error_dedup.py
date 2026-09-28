"""Unit tests for consecutive-error dedup in append_history_record.

A scheduled task that fails repeatedly with the same error (e.g. a stale
model credential) must not keep appending identical chat.error records to
session history. The dedup state is keyed per session and resets on a
non-error assistant record (a successful round) or a changed error text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import session_history as sh
from jiuwenswarm.server.runtime.session import session_metadata


ERROR_401 = (
    "[181001] model call failed, reason: openAI API async stream error: "
    "AuthenticationError: Error code: 401 - {'error_msg': 'AppKey or AppSecret is invalid'}"
)


@pytest.fixture(autouse=True)
def _clean_dedup_state() -> None:
    sh._error_dedup_state.clear()
    yield
    sh._error_dedup_state.clear()


@pytest.fixture
def isolated_sessions_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    return sessions_root


def _append(sid: str, rid: str, event_type: str, content: str) -> sh.HistoryAppendStatus:
    return sh.append_history_record(
        session_id=sid,
        request_id=rid,
        channel_id="acp",
        role="assistant",
        event_type=event_type,
        content=content,
        timestamp=1000.0,
        sessions_root=None,
    )


def test_first_error_persists_second_identical_suppressed(isolated_sessions_root: Path) -> None:
    assert _append("dedup-sess-1", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-1", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE


def test_different_error_text_is_not_suppressed(isolated_sessions_root: Path) -> None:
    assert _append("dedup-sess-2", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-2", "r2", "chat.error", "rate limited") is sh.HistoryAppendStatus.PERSISTED


def test_different_session_is_not_suppressed(isolated_sessions_root: Path) -> None:
    assert _append("dedup-sess-3", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-4", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED


def test_success_final_resets_dedup_state(isolated_sessions_root: Path) -> None:
    assert _append("dedup-sess-5", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-5", "r2", "chat.final", "天气晴朗，28度") is sh.HistoryAppendStatus.PERSISTED
    # Same error again after a successful round must persist again.
    assert _append("dedup-sess-5", "r3", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED


def test_whitespace_normalized_fingerprint(isolated_sessions_root: Path) -> None:
    assert _append("dedup-sess-6", "r1", "chat.error", "  boom\n\n  boom  ") is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-6", "r2", "chat.error", "boom boom") is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE


def test_cron_final_error_like_is_deduped(isolated_sessions_root: Path) -> None:
    cron_err = "[cron] 任务执行失败: boom"
    assert _append("dedup-sess-7", "r1", "chat.final", cron_err) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-7", "r2", "chat.final", cron_err) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE


def test_plain_final_not_treated_as_error(isolated_sessions_root: Path) -> None:
    assert _append("dedup-sess-8", "r1", "chat.final", "查询结果正常") is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-8", "r2", "chat.final", "查询结果正常") is sh.HistoryAppendStatus.PERSISTED


def test_disabled_via_env_writes_all(isolated_sessions_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sh.ERROR_DEDUP_ENABLED_ENV, "0")
    assert _append("dedup-sess-9", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-9", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED


def test_user_role_records_never_go_through_error_dedup(isolated_sessions_root: Path) -> None:
    assert sh.append_history_record(
        session_id="dedup-sess-10",
        request_id="r1",
        channel_id="acp",
        role="user",
        content="定时查询天气",
        timestamp=1000.0,
    ) is sh.HistoryAppendStatus.PERSISTED
    assert sh.append_history_record(
        session_id="dedup-sess-10",
        request_id="r2",
        channel_id="acp",
        role="user",
        content="定时查询天气",
        timestamp=2000.0,
    ) is sh.HistoryAppendStatus.PERSISTED


def test_user_record_does_not_resets_dedup_state(isolated_sessions_root: Path) -> None:
    # See test_user_record_never_resets_dedup below — this older variant
    # was retired when dedup was relaxed from "cron only" to "all sources".
    # Kept here as a tombstone for the historical behaviour.
    assert sh.append_history_record(
        session_id="dedup-sess-11",
        request_id="interactive-1",
        channel_id="acp",
        role="user",
        content="能帮我看看吗",
        timestamp=1500.0,
    ) is sh.HistoryAppendStatus.PERSISTED


def test_user_record_never_resets_dedup(isolated_sessions_root: Path) -> None:
    # The facade writes a user-role record at the start of every request —
    # interactive or cron. As of this revision the consecutive-error dedup
    # applies to ALL sources, so the user-role record must NOT clear the
    # dedup counter. Otherwise two consecutive reminder ticks with the
    # same fingerprint would both surface their chat.error to the wire.
    assert _append("dedup-sess-bg-1", "cron-1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert sh.append_history_record(
        session_id="dedup-sess-bg-1",
        request_id="cron-2",
        channel_id="acp",
        role="user",
        content="定时查询天气",
        timestamp=1500.0,
    ) is sh.HistoryAppendStatus.PERSISTED
    # Same fingerprint error in the next cron tick — must still be suppressed.
    assert _append("dedup-sess-bg-1", "cron-2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE

    # Interactive user retry in the same session — by current spec the
    # user accepts "first error surfaces, subsequent identical errors
    # are deduped" across the whole session. The user-role record must
    # not reset.
    assert sh.append_history_record(
        session_id="dedup-sess-bg-1",
        request_id="interactive-1",
        channel_id="acp",
        role="user",
        content="能帮我看看吗",
        timestamp=2000.0,
    ) is sh.HistoryAppendStatus.PERSISTED
    assert _append(
        "dedup-sess-bg-1", "interactive-1", "chat.error", ERROR_401
    ) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE


def test_in_round_tool_call_does_not_reset_dedup(isolated_sessions_root: Path) -> None:
    # First tick: error before any tools — state initialized.
    assert _append("dedup-sess-12", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    # Next tick: agent ran a tool then hit the same error. The tool_call event
    # between rounds used to reset the state, breaking the dedup mechanism.
    assert _append("dedup-sess-12", "r2", "chat.tool_call", '{"name":"x"}') is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-12", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE
    # Third tick: more mid-round events but same error fingerprint.
    assert _append("dedup-sess-12", "r3", "chat.file", "{}") is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-12", "r3", "chat.error", ERROR_401) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE


def test_empty_chat_error_returns_skipped_empty_not_duplicate(isolated_sessions_root: Path) -> None:
    # Heartbeat / empty-payload skips must not be mis-classified as
    # SUPPRESSED_DUPLICATE — facade treats those as "duplicate" and would
    # wrongly swallow them on the wire.
    status = sh.append_history_record(
        session_id="dedup-sess-13",
        request_id="r1",
        channel_id="acp",
        role="assistant",
        event_type="chat.error",
        content="",
        timestamp=1000.0,
    )
    assert status is sh.HistoryAppendStatus.SKIPPED_EMPTY


def test_heartbeat_session_returns_skipped_heartbeat_not_duplicate(isolated_sessions_root: Path) -> None:
    status = sh.append_history_record(
        session_id="heartbeat-foo",
        request_id="r1",
        channel_id="acp",
        role="assistant",
        event_type="chat.error",
        content=ERROR_401,
        timestamp=1000.0,
    )
    assert status is sh.HistoryAppendStatus.SKIPPED_HEARTBEAT


def test_max_repeats_threshold(isolated_sessions_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sh.ERROR_DEDUP_MAX_REPEATS_ENV, "2")
    assert _append("dedup-sess-14", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    assert _append("dedup-sess-14", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    # Third occurrence now exceeds threshold.
    assert _append("dedup-sess-14", "r3", "chat.error", ERROR_401) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE


def test_lru_eviction(isolated_sessions_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sh, "ERROR_DEDUP_STATE_MAX", 4)
    for i in range(6):
        assert _append(f"dedup-sess-lru-{i}", "r1", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    # Oldest entries must have been evicted; re-touching dedup-sess-lru-0
    # must count as first occurrence again.
    assert _append("dedup-sess-lru-0", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.PERSISTED
    # Newly-touched sessions (lru-4, lru-5) are still tracked — their second
    # occurrence must be suppressed.
    assert _append("dedup-sess-lru-5", "r2", "chat.error", ERROR_401) is sh.HistoryAppendStatus.SUPPRESSED_DUPLICATE
