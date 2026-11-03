"""Queue manager proposals must execute through real scoped operations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import queue_manager
from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import parse_qwen_omni_tool_call, qwen_omni_tools


def management_call():
    return parse_qwen_omni_tool_call({
        "name": "jiuwen_queue_manage", "call_id": "manage-1",
        "arguments": {"instruction": "先打开飞书，再打开微信"},
    })


def test_qwen_exposes_queue_manager_and_preserves_voice_approval_answers():
    names = {t["function"]["name"] for t in qwen_omni_tools(queue_manager=True)}
    assert names == {"jiuwen_delegate", "jiuwen_queue_manage", "jiuwen_task_answer"}
    for args in ({}, {"instruction": " "}, {"instruction": "move", "job_id": "invented"}):
        with pytest.raises(ValueError):
            parse_qwen_omni_tool_call({"name": "jiuwen_queue_manage", "call_id": "m", "arguments": args})


@pytest.mark.asyncio
async def test_reorder_queries_then_executes_and_returns_real_receipts(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(side_effect=[
        {"jobs": [{"job_id": "wechat", "revision": 1}, {"job_id": "feishu", "revision": 1}]},
        {"operation": {"state": "reordered"}},
    ]))
    step = AsyncMock(side_effect=[
        json.dumps({"tool": {"name": "jiuwen_task_reorder", "arguments": {
            "job_id": "feishu", "action": "before", "before_job_id": "wechat"}}}),
        json.dumps({"response": "已将飞书排在微信前面。"}),
    ])
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "先飞书再微信")
    calls = manager.operate.call_args_list
    assert calls[0].args[2].name == "jiuwen_task_query"
    assert calls[1].args[:2] == ("alice", "voice")
    assert calls[1].args[2].arguments == {"job_id": "feishu", "action": "before", "before_job_id": "wechat"}
    assert result["receipts"][1]["receipt"]["operation"]["state"] == "reordered"


@pytest.mark.asyncio
async def test_unknown_ids_and_new_delegations_cannot_execute(monkeypatch):
    for tool in [
        {"name": "jiuwen_task_cancel", "arguments": {"job_id": "guessed"}},
        {"name": "jiuwen_delegate", "arguments": {"task": "new"}},
    ]:
        manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
        monkeypatch.setattr(queue_manager, "request_step", AsyncMock(return_value=json.dumps({"tool": tool})))
        result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "取消任务")
        assert result["state"] == "incomplete"
        assert manager.operate.await_count == 1


@pytest.mark.asyncio
async def test_joyai_manager_cannot_submit_information_answers(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": [{"job_id": "form", "revision": 1}]}))
    step = AsyncMock(return_value=json.dumps({"tool": {"name": "jiuwen_task_answer", "arguments": {
        "job_id": "form", "interaction_id": "question", "answers": ["yes"]}}}))
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(
        manager, "alice", "voice", management_call(), "回答问题", allow_answers=False, reply_language="en",
    )
    assert result["state"] == "incomplete"
    assert manager.operate.await_count == 1
    context = json.loads(step.call_args.args[0][1]["content"])
    assert context["reply_language"] == "en"
    assert "jiuwen_task_answer" not in [t["function"]["name"] for t in context["allowed_tools"]]


@pytest.mark.asyncio
async def test_failure_after_write_preserves_receipt_without_repeating(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(side_effect=[
        {"jobs": [{"job_id": "feishu", "revision": 1}]}, {"state": "cancelling"},
    ]))
    monkeypatch.setattr(queue_manager, "request_step", AsyncMock(side_effect=[
        json.dumps({"tool": {"name": "jiuwen_task_cancel", "arguments": {"job_id": "feishu"}}}),
        TimeoutError(),
    ]))
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "取消飞书任务")
    assert result["state"] == "incomplete"
    assert result["receipts"][-1]["receipt"]["state"] == "cancelling"
    assert manager.operate.await_count == 2
