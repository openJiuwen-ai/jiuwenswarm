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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["", "not json", '{"tool":{"name":[],"arguments":{}}}',
    '{"tool":{"name":"jiuwen_task_modify","arguments":{"job_id":"a","revision":0,"instruction":"change"}}}'])
async def test_invalid_proposal_is_corrected_without_user_error(monkeypatch, invalid):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": [{"job_id": "a", "revision": 1}]}))
    step = AsyncMock(side_effect=[invalid, '{"response":"已查询当前任务。"}'])
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "查询任务")
    assert "error" not in result
    assert result["response"] == "已查询当前任务。"
    assert manager.operate.await_count == 1
    assert json.loads(step.call_args.args[0][-1]["content"])["applied"] is False


@pytest.mark.asyncio
async def test_duplicate_write_reuses_receipt_without_dispatch(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(side_effect=[
        {"jobs": [{"job_id": "a", "revision": 1}]}, {"state": "cancelling"},
    ]))
    write = json.dumps({"tool": {"name": "jiuwen_task_cancel", "arguments": {"job_id": "a"}}})
    monkeypatch.setattr(queue_manager, "request_step", AsyncMock(side_effect=[write, write, '{"response":"正在停止。"}']))
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "取消任务")
    assert "error" not in result
    assert manager.operate.await_count == 2
    assert result["receipts"][1]["receipt"] == result["receipts"][2]["receipt"]


@pytest.mark.asyncio
async def test_operational_exception_is_not_replayed_as_validation_error(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(side_effect=[
        {"jobs": [{"job_id": "a", "revision": 1}]}, RuntimeError("connection lost after dispatch"),
    ]))
    step = AsyncMock(return_value=json.dumps({"tool": {
        "name": "jiuwen_task_cancel", "arguments": {"job_id": "a"},
    }}))
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "取消任务")
    assert result["state"] == "incomplete"
    assert result["error"] == "connection lost after dispatch"
    assert manager.operate.await_count == 2
    assert step.await_count == 1


@pytest.mark.asyncio
async def test_revision_conflict_can_query_then_correct_without_replaying_old_write(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(side_effect=[
        {"jobs": [{"job_id": "a", "revision": 1}]},
        {"state": "rejected", "applied": False, "error": "revision changed"},
        {"jobs": [{"job_id": "a", "revision": 2}]},
        {"state": "accepted"},
    ]))
    def modification(revision):
        return json.dumps({"tool": {"name": "jiuwen_task_modify", "arguments": {
            "job_id": "a", "revision": revision, "instruction": "change",
        }}})

    monkeypatch.setattr(queue_manager, "request_step", AsyncMock(side_effect=[
        modification(1), json.dumps({"tool": {"name": "jiuwen_task_query", "arguments": {}}}),
        modification(2), '{"response":"修改已受理。"}',
    ]))
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "修改任务")
    assert "error" not in result
    assert manager.operate.await_count == 4
    assert manager.operate.await_args_list[1].args[2].arguments["revision"] == 1
    assert manager.operate.await_args_list[3].args[2].arguments["revision"] == 2


@pytest.mark.asyncio
async def test_invalid_proposal_correction_budget_is_bounded(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    step = AsyncMock(return_value="invalid")
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "查询任务")
    assert result["state"] == "incomplete"
    assert step.await_count == 4
    assert manager.operate.await_count == 1


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


@pytest.mark.asyncio
async def test_pending_management_is_cancellable_and_does_not_repeat_operations(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending_step(messages):
        entered.set()
        await release.wait()
        return json.dumps({"response": "队列为空"})

    monkeypatch.setattr(queue_manager, "request_step", pending_step)
    pending = asyncio.create_task(queue_manager.manage_queue(
        manager, "alice", "voice", management_call(), "查询队列",
    ))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert manager.operate.await_count == 1


@pytest.mark.asyncio
async def test_management_still_stops_after_six_model_decisions(monkeypatch):
    manager = SimpleNamespace(operate=AsyncMock(return_value={"jobs": []}))
    step = AsyncMock(return_value=json.dumps({
        "tool": {"name": "jiuwen_task_query", "arguments": {}},
    }))
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(manager, "alice", "voice", management_call(), "查询队列")
    assert result["state"] == "incomplete"
    assert "operation limit" in result["error"]
    assert step.await_count == 6


@pytest.mark.asyncio
async def test_model_request_preserves_configured_network_timeout(monkeypatch):
    from jiuwenswarm.common import config
    from openjiuwen.core.foundation import llm

    monkeypatch.delenv("DUPLEX_QUEUE_MANAGER_MODEL", raising=False)
    monkeypatch.setattr(config, "get_default_models", lambda: [{
        "model_client_config": {
            "model_name": "test-model", "client_provider": "OpenAI",
            "api_key": "test-only", "api_base": "https://example.invalid", "timeout": 77,
        },
    }])
    received = []
    invoke = AsyncMock(return_value=SimpleNamespace(content='{"response":"done"}'))

    def model(**kwargs):
        received.append(kwargs["model_client_config"])
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(llm, "Model", model)
    assert await queue_manager.request_step([]) == '{"response":"done"}'
    assert received[0].timeout == 77
    invoke.assert_awaited_once_with([])
