"""Full-duplex spoken-receipt markers must not reach persisted chat history."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface

ANSWER = "1 + 1 = 2"
RECEIPT = (
    f"{ANSWER}\n\n[[JIUWEN_BRIEF_BEGIN:task-7]]\n计算完成，结果是 2。\n[[JIUWEN_BRIEF_END:task-7]]"
)


def _request(channel_id="video_tool", binding=None) -> AgentRequest:
    params = {"mode": "agent"}
    if binding is not None:
        params["managed_task_binding"] = binding
    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        session_id="task-duplex:1",
        req_method=ReqMethod.CHAT_SEND,
        params=params,
    )


@pytest.fixture
def records(monkeypatch):
    written = []

    async def fake_io(fn, **kwargs):
        written.append(kwargs)

    monkeypatch.setattr(interface, "_run_history_io", fake_io)
    return written


@pytest.mark.asyncio
async def test_duplex_task_final_is_persisted_without_receipt(records) -> None:
    request = _request(binding={"endpoint": "gw", "task_id": "task-7"})
    await interface.JiuWenSwarm._persist_control_chunk_to_history(
        None,
        SimpleNamespace(payload={"event_type": "chat.final", "content": RECEIPT}),
        request=request,
        session_id="task-duplex:1",
        rid="req-1",
        cid="video_tool",
        pending_final_chunks=[],
        pending_final_started_at_holder=[None],
    )
    await interface.JiuWenSwarm._persist_pending_final_text_control(
        None,
        request=request,
        session_id="task-duplex:1",
        rid="req-1",
        cid="video_tool",
        pending_final_chunks=[RECEIPT],
        segment_started_at=None,
    )
    finals = [r["content"] for r in records if r.get("event_type") == "chat.final"]
    assert finals == [ANSWER, ANSWER]


@pytest.mark.parametrize(
    "request_",
    [
        _request(binding={"endpoint": "gw", "task_id": "other-task"}),
        _request(),
        _request(channel_id="web", binding={"endpoint": "gw", "task_id": "task-7"}),
    ],
)
def test_only_the_bound_duplex_task_is_stripped(request_) -> None:
    assert interface._history_answer_text(request_, RECEIPT) == RECEIPT
