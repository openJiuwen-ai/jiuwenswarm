# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent Agent identity and exclusive Session lease boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import session_guard


def _definition(instructions: str = "Answer concisely.") -> dict:
    return {"name": "guard_agent", "instructions": instructions, "tools": "*"}


def test_binding_survives_new_process_and_rejects_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_guard, "get_agent_sessions_dir", lambda: tmp_path)

    session_guard.bind_agent("session-1", _definition(), resumed=False)
    stored = json.loads(
        next((tmp_path / ".process_cli_bindings").glob("*.json")).read_text()
    )
    assert len(stored["fingerprint"]) == 64
    session_guard.bind_agent("session-1", _definition(), resumed=True)

    with pytest.raises(session_guard.SessionGuardError) as caught:
        session_guard.bind_agent("session-1", _definition("Different."), resumed=True)
    assert caught.value.code == "AGENT_DEFINITION_SESSION_CONFLICT"

    with pytest.raises(session_guard.SessionGuardError) as caught:
        session_guard.bind_agent("session-1", None, resumed=True)
    assert caught.value.code == "AGENT_DEFINITION_SESSION_CONFLICT"


def test_session_lease_is_exclusive_and_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_guard, "get_agent_sessions_dir", lambda: tmp_path)
    first = session_guard.SessionLease("session-1")
    second = session_guard.SessionLease("session-1")
    first.acquire()
    try:
        with pytest.raises(session_guard.SessionGuardError) as caught:
            second.acquire()
        assert caught.value.code == "SESSION_BUSY"
        assert caught.value.retryable
    finally:
        first.release()
    second.acquire()
    second.release()
