"""Unit tests for consecutive-error dedup in append_history_record.

A scheduled task that fails repeatedly with the same error (e.g. a stale
model credential) must not keep appending identical chat.error records to
session history. The dedup state is keyed per session and resets on a
non-error assistant record (a successful round) or a changed error text.
"""
from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.session import session_history as sh


ERROR_401 = (
    "[181001] model call failed, reason: openAI API async stream error: "
    "AuthenticationError: Error code: 401 - {'error_msg': 'AppKey or AppSecret is invalid'}"
)


@pytest.fixture(autouse=True)
def _clean_dedup_state() -> None:
    sh._error_dedup_state.clear()
    yield
    sh._error_dedup_state.clear()


def _append(sid: str, rid: str, event_type: str, content: str) -> bool:
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


def test_first_error_persists_second_identical_suppressed() -> None:
    assert _append("dedup-sess-1", "r1", "chat.error", ERROR_401) is True
    assert _append("dedup-sess-1", "r2", "chat.error", ERROR_401) is False


def test_different_error_text_is_not_suppressed() -> None:
    assert _append("dedup-sess-2", "r1", "chat.error", ERROR_401) is True
    assert _append("dedup-sess-2", "r2", "chat.error", "rate limited") is True


def test_different_session_is_not_suppressed() -> None:
    assert _append("dedup-sess-3", "r1", "chat.error", ERROR_401) is True
    assert _append("dedup-sess-4", "r2", "chat.error", ERROR_401) is True


def test_success_final_resets_dedup_state() -> None:
    assert _append("dedup-sess-5", "r1", "chat.error", ERROR_401) is True
    assert _append("dedup-sess-5", "r2", "chat.final", "天气晴朗，28度") is True
    # Same error again after a successful round must persist again.
    assert _append("dedup-sess-5", "r3", "chat.error", ERROR_401) is True


def test_whitespace_normalized_fingerprint() -> None:
    assert _append("dedup-sess-6", "r1", "chat.error", "  boom\n\n  boom  ") is True
    assert _append("dedup-sess-6", "r2", "chat.error", "boom boom") is False


def test_cron_final_error_like_is_deduped() -> None:
    assert _append("dedup-sess-7", "r1", "chat.final", "[cron] 任务执行失败: boom") is True
    assert _append("dedup-sess-7", "r2", "chat.final", "[cron] 任务执行失败: boom") is False


def test_plain_final_not_treated_as_error() -> None:
    assert _append("dedup-sess-8", "r1", "chat.final", "查询结果正常") is True
    assert _append("dedup-sess-8", "r2", "chat.final", "查询结果正常") is True


def test_disabled_via_env_writes_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sh.ERROR_DEDUP_ENABLED_ENV, "0")
    assert _append("dedup-sess-9", "r1", "chat.error", ERROR_401) is True
    assert _append("dedup-sess-9", "r2", "chat.error", ERROR_401) is True


def test_user_role_records_never_go_through_error_dedup() -> None:
    assert sh.append_history_record(
        session_id="dedup-sess-10",
        request_id="r1",
        channel_id="acp",
        role="user",
        content="定时查询天气",
        timestamp=1000.0,
    ) is True
    assert sh.append_history_record(
        session_id="dedup-sess-10",
        request_id="r2",
        channel_id="acp",
        role="user",
        content="定时查询天气",
        timestamp=2000.0,
    ) is True
