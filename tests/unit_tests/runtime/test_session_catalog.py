# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import FrozenInstanceError
from inspect import Parameter, signature
from unittest.mock import Mock

import pytest

from jiuwenswarm.runtime import session_catalog
from jiuwenswarm.runtime.session_catalog import (
    SessionCatalogError,
    SessionGetInput,
    SessionListInput,
    SessionSummary,
    get_session,
    list_sessions,
)


def _metadata(
    session_id: str,
    *,
    channel_id: str = "process_cli",
    mode: str = "agent.work.normal",
    last_message_at: object = 0,
) -> dict:
    return {
        "session_id": session_id,
        "channel_id": channel_id,
        "title": f"title-{session_id}",
        "mode": mode,
        "work_mode": "code" if "code" in mode else "work",
        "project_id": "project",
        "project_dir": "D:/project",
        "model": "model#0",
        "created_at": 1,
        "last_message_at": last_message_at,
        "message_count": 2,
        "delivery_context": {"secret": "must-not-leak"},
        "channel_metadata": {"secret": "must-not-leak"},
    }


def test_list_filters_before_sorting_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collected = Mock(
        return_value=iter(
            [
                _metadata("wrong-channel", channel_id="tui", last_message_at=100),
                _metadata("team", mode="team.work.normal", last_message_at=90),
                _metadata("legacy", mode="agent", last_message_at=8),
                _metadata("b", mode="code.normal", last_message_at=10),
                _metadata("a", mode="agent.code.normal", last_message_at=10),
                _metadata("unknown", mode="unknown", last_message_at=80),
                _metadata("unsafe/session", last_message_at=70),
            ]
        )
    )
    monkeypatch.setattr(session_catalog, "_collect_session_metadata", collected)

    result = list_sessions(
        SessionListInput(channel_id="PROCESS_CLI", limit=1, offset=1)
    )

    collected.assert_called_once_with()
    assert result.total == 3
    assert [item.session_id for item in result.sessions] == ["b"]
    assert result.sessions[0].mode == "agent.code.normal"
    assert "must-not-leak" not in str(result.to_dict())


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (_metadata("owned", mode="code.normal"), "owned"),
        (_metadata("remote", channel_id="tui"), None),
        (_metadata("team", mode="team.code.normal"), None),
    ],
)
def test_get_enforces_channel_and_single_agent_scope(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict,
    expected: str | None,
) -> None:
    reader = Mock(return_value=metadata)
    monkeypatch.setattr(session_catalog, "_read_session_metadata", reader)

    result = get_session(
        SessionGetInput(channel_id="process_cli", session_id=metadata["session_id"])
    )

    reader.assert_called_once_with(metadata["session_id"])
    assert (result.session_id if result is not None else None) == expected
    if result is not None:
        assert result.mode == "agent.code.normal"


def test_get_uses_cache_bust_without_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.session import session_metadata

    reader = Mock(return_value={})
    monkeypatch.setattr(session_metadata, "get_session_metadata", reader)

    assert session_catalog._read_session_metadata("safe-session") == {}
    reader.assert_called_once_with(
        "safe-session",
        cache_bust=True,
        enable_writeback=False,
    )


@pytest.mark.parametrize(
    "list_input",
    [
        SessionListInput(channel_id="", limit=20, offset=0),
        SessionListInput(channel_id="process_cli", limit=True, offset=0),
        SessionListInput(channel_id="process_cli", limit=201, offset=0),
        SessionListInput(channel_id="process_cli", limit=20, offset=-1),
    ],
)
def test_list_rejects_invalid_scope_or_page_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    list_input: SessionListInput,
) -> None:
    collector = Mock(side_effect=AssertionError("storage must not be read"))
    monkeypatch.setattr(session_catalog, "_collect_session_metadata", collector)

    with pytest.raises(SessionCatalogError) as caught:
        list_sessions(list_input)

    assert caught.value.code == "BAD_REQUEST"
    collector.assert_not_called()


@pytest.mark.parametrize("session_id", ("../secret", "nested/session", ".", "a" * 81))
def test_get_rejects_unsafe_session_id_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
) -> None:
    reader = Mock(side_effect=AssertionError("storage must not be read"))
    monkeypatch.setattr(session_catalog, "_read_session_metadata", reader)

    with pytest.raises(SessionCatalogError, match="invalid session_id") as caught:
        get_session(SessionGetInput(channel_id="process_cli", session_id=session_id))

    assert caught.value.code == "BAD_REQUEST"
    reader.assert_not_called()


def test_storage_error_is_sanitized() -> None:
    secret = "D:/private/session?token=must-not-leak"
    original = session_catalog._collect_session_metadata
    session_catalog._collect_session_metadata = Mock(side_effect=OSError(secret))
    try:
        with pytest.raises(SessionCatalogError) as caught:
            list_sessions(SessionListInput(channel_id="process_cli"))
    finally:
        session_catalog._collect_session_metadata = original

    assert caught.value.code == "READ_FAILED"
    assert str(caught.value) == "failed to list sessions"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_session_dto_is_frozen_slotted_and_keyword_only() -> None:
    summary = SessionSummary(
        session_id="session",
        channel_id="process_cli",
        title="title",
        mode="agent.work.normal",
        work_mode="work",
    )

    assert not hasattr(summary, "__dict__")
    with pytest.raises(FrozenInstanceError):
        summary.title = "changed"  # type: ignore[misc]
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        for parameter in signature(SessionSummary).parameters.values()
    )
