# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live completion gate for the Process CLI Session Runtime chain."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import app
from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session import RuntimeSessionState, SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState
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
        snapshot = client.runtime.session_coordinator.snapshot_session(session_id)
        assert snapshot is not None
        assert snapshot.state is RuntimeSessionState.CLOSED
        assert all(execution.state.terminal for execution in snapshot.executions)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason="requires a configured model and interactive ask_user support",
)
@pytest.mark.parametrize(
    ("goal", "expected_work_kind", "expected_text"),
    [
        (False, SessionWorkKind.CHAT_STREAM, "ASK_USER_RESUME_OK"),
        (True, SessionWorkKind.GOAL_STREAM, "GOAL_ASK_USER_RESUME_OK"),
    ],
)
async def test_single_agent_ask_user_resume_after_stream_end_live(
    tmp_path: Path,
    goal: bool,
    expected_work_kind: SessionWorkKind,
    expected_text: str,
) -> None:
    client = InProcessRuntimeClient()
    session_id = ""
    try:
        await client.start()
        session_id = await client.create_or_resume_session(
            channel_id="web",
            session_id=None,
        )
        instruction = (
            "你必须先调用 ask_user，让我在 RED 和 BLUE 中选择一个；"
            f"收到选择后只回复 {expected_text}。"
        )
        params = {
            "mode": "agent.work.normal",
            "work_mode": "work",
            "project_dir": str(tmp_path),
            "cwd": str(tmp_path),
            "trusted_dirs": [str(tmp_path)],
            "supports_user_interaction": True,
        }
        if goal:
            params.update({"action": "set", "objective": instruction})
        else:
            params["query"] = instruction
        original = AgentRequest(
            request_id="live-ask-user-original",
            channel_id="web",
            session_id=session_id,
            req_method=ReqMethod.COMMAND_GOAL if goal else ReqMethod.CHAT_SEND,
            is_stream=True,
            params=params,
        )
        first_events = [
            event
            async for event in client.stream(original)
        ]
        waiting = client.runtime.session_coordinator.snapshot_session(session_id)
        assert waiting is not None
        execution = next(
            execution
            for execution in waiting.executions
            if execution.state is SessionExecutionState.WAITING_FOR_CONTROL
            and execution.work_kind is expected_work_kind
        )
        interaction = next(
            event
            for event in first_events
            if event.event_type == "chat.ask_user_question"
            and (event.payload or {}).get("request_id")
            == execution.waiting_control_id
        )

        interaction_payload = interaction.payload or {}
        answer = AgentRequest(
            request_id="live-ask-user-answer",
            channel_id="web",
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={
                "query": "",
                "request_id": interaction_payload["request_id"],
                "answers": [
                    {
                        "question": "请选择颜色",
                        "selected_options": ["BLUE"],
                    }
                ],
                "source": "ask_user_interrupt",
                "mode": "agent.work.normal",
                "work_mode": "work",
                "project_dir": str(tmp_path),
                "cwd": str(tmp_path),
                "trusted_dirs": [str(tmp_path)],
                "supports_user_interaction": True,
            },
        )
        resumed_events = await asyncio.wait_for(
            _collect_runtime_events(client.stream(answer)),
            timeout=180,
        )
        resumed_text = "".join(
            str((event.payload or {}).get("content") or "")
            for event in resumed_events
        )
        assert expected_text in resumed_text
        completed = client.runtime.session_coordinator.snapshot_session(session_id)
        assert completed is not None
        assert all(execution.state.terminal for execution in completed.executions)
    finally:
        if session_id:
            await client.cleanup_session(channel_id="web", session_id=session_id)
        await client.close()


async def _collect_runtime_events(stream):
    return [event async for event in stream]
