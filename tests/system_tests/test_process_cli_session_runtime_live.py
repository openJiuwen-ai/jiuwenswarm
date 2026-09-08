# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live completion gate for the managed Process CLI reference chain."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import app
from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.runtime.session import RuntimeSessionState, SessionManagementMode
from jiuwenswarm.server.runtime.session.session_history import load_history_records

pytestmark = [pytest.mark.system, pytest.mark.slow]

_LIVE_ENABLED = os.environ.get("RUN_LIVE_SESSION_RUNTIME_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}


class _RecordingClient(InProcessRuntimeClient):
    instances: list[_RecordingClient] = []

    def __init__(self) -> None:
        super().__init__()
        type(self).instances.append(self)


def _args(
    workspace: Path,
    *,
    session_id: str | None,
    mode: str,
    work_mode: str,
    prompt: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        prompt=prompt,
        session=session_id,
        cwd=str(workspace),
        project_dir=str(workspace),
        trusted_dir=[str(workspace)],
        mode=mode,
        work_mode=work_mode,
        output="json",
        timeout=180.0,
        show_reasoning=False,
        show_tools=False,
        _interactive_worker=False,
        _session_result_file=None,
    )


def _assert_terminal_response(document: dict) -> None:
    events = document.get("events") or []
    assert document.get("ok") is True
    assert any(bool(event.get("is_complete")) for event in events)
    response_text = "".join(
        str((event.get("payload") or {}).get(key) or "")
        for event in events
        for key in ("delta", "content", "text", "answer")
    ).strip()
    assert response_text


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason=(
        "requires a configured model; set RUN_LIVE_SESSION_RUNTIME_TESTS=1 "
        "to run the Session Runtime completion gate"
    ),
)
@pytest.mark.parametrize(
    ("mode", "work_mode"),
    [("agent.work.normal", "work"), ("agent.code.normal", "code")],
)
async def test_process_cli_two_turn_session_resume_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    work_mode: str,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", _RecordingClient)
    _RecordingClient.instances.clear()
    first_output = io.StringIO()
    first_result = await app.run(
        _args(
            tmp_path,
            session_id=None,
            mode=mode,
            work_mode=work_mode,
            prompt="只回复：SESSION_RUNTIME_FIRST_OK",
        ),
        stdout=first_output,
        stderr=io.StringIO(),
    )
    assert first_result == 0
    first_document = json.loads(first_output.getvalue())
    session_id = str(first_document["session_id"])
    assert session_id
    _assert_terminal_response(first_document)
    first_history = load_history_records(session_id)
    assert first_history

    second_output = io.StringIO()
    second_result = await app.run(
        _args(
            tmp_path,
            session_id=session_id,
            mode=mode,
            work_mode=work_mode,
            prompt="只回复：SESSION_RUNTIME_SECOND_OK",
        ),
        stdout=second_output,
        stderr=io.StringIO(),
    )
    assert second_result == 0
    second_document = json.loads(second_output.getvalue())
    assert second_document["session_id"] == session_id
    _assert_terminal_response(second_document)
    second_history = load_history_records(session_id)
    assert len(second_history) > len(first_history)

    assert len(_RecordingClient.instances) == 2
    for client in _RecordingClient.instances:
        assert client.runtime.session_management_mode is SessionManagementMode.RUNTIME_MANAGED
        snapshot = client.runtime.session_coordinator.snapshot_session(session_id)
        assert snapshot is not None
        assert snapshot.state is RuntimeSessionState.CLOSED
        assert all(execution.state.terminal for execution in snapshot.executions)
