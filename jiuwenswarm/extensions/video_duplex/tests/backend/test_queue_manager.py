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


def test_qwen_exposes_queue_manager_instead_of_low_level_controls():
    names = {t["function"]["name"] for t in qwen_omni_tools(queue_manager=True)}
    assert names == {"jiuwen_delegate", "jiuwen_queue_manage"}
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


@pytest.mark.asyncio
async def test_gateway_duplicate_call_shares_execution(monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager

    channel = SimpleNamespace(send_response=AsyncMock())
    manager = VideoSearchManager(channel, None, log_event=lambda *a, **k: None, qwen_active=lambda: True)
    manager.scope = AsyncMock(return_value=("alice", "voice"))
    gate = asyncio.Event()

    async def run(*args, **kwargs):
        await gate.wait()
        return {"managed_by": "text_queue_manager", "receipts": []}

    mocked = AsyncMock(side_effect=run)
    monkeypatch.setattr(queue_manager, "manage_queue", mocked)
    params = {"name": "jiuwen_queue_manage", "call_id": "m", "arguments": {"instruction": "调整顺序"}}
    tasks = [asyncio.create_task(manager.handle_qwen_tool(None, str(i), params, "")) for i in range(2)]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    assert mocked.await_count == 1
    assert channel.send_response.await_count == 2
    await manager.close()


@pytest.mark.asyncio
async def test_text_manager_changes_real_queue_order(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend.task_adapter import VideoSearchManager
    from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore

    service = TaskService(TaskStore(tmp_path / "tasks.sqlite"), None)
    service.started = True
    service.kick = lambda: None
    wechat = service.submit("alice", "voice", "create-wechat", "打开微信")
    feishu = service.submit("alice", "voice", "create-feishu", "打开飞书")
    manager = VideoSearchManager(None, None, log_event=lambda *a, **k: None, qwen_active=lambda: True)
    manager._service = service
    monkeypatch.setattr(queue_manager, "request_step", AsyncMock(side_effect=[
        json.dumps({"tool": {"name": "jiuwen_task_reorder", "arguments": {
            "job_id": feishu["id"], "action": "before", "before_job_id": wechat["id"]}}}),
        json.dumps({"response": "飞书已排在微信前面"}),
    ]))
    try:
        result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "先飞书再微信")
        assert "state" not in result
        tasks, _ = service.snapshot("alice", "voice")
        assert [t["id"] for t in sorted(tasks, key=lambda t: t["position"])] == [feishu["id"], wechat["id"]]
        assert result["receipts"][-1]["receipt"]["operation"]["state"] == "reordered"
    finally:
        await manager.close()
