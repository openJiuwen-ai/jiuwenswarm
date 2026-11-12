"""Queue manager proposals must execute through real scoped operations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import queue_manager
from jiuwenswarm.extensions.video_duplex.tests.backend.task_test_support import management_call, run_queue, tool_proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["", "not json", '{"tool":{"name":[],"arguments":{}}}',
    '{"tool":{"name":"jiuwen_task_modify","arguments":{"job_id":"a","revision":0,"instruction":"change"}}}'])
async def test_invalid_proposal_is_corrected_without_user_error(monkeypatch, invalid):
    result, operate, step = await run_queue(monkeypatch, [invalid, '{"response":"已查询当前任务。"}'],
                                          {"jobs": [{"job_id": "a", "revision": 1}]})
    assert "error" not in result
    assert result["response"] == "已查询当前任务。"
    assert operate.await_count == 1
    assert json.loads(step.call_args.args[0][-1]["content"])["applied"] is False


@pytest.mark.asyncio
async def test_duplicate_write_reuses_receipt_without_dispatch(monkeypatch):
    write = tool_proposal("jiuwen_task_cancel", {"job_id": "a"})
    result, operate, _ = await run_queue(monkeypatch, [write, write, '{"response":"正在停止。"}'],
                                        [{"jobs": [{"job_id": "a", "revision": 1}]}, {"state": "cancelling"}])
    assert "error" not in result
    assert operate.await_count == 2
    assert result["receipts"][1]["receipt"] == result["receipts"][2]["receipt"]


@pytest.mark.asyncio
async def test_operational_exception_is_not_replayed_as_validation_error(monkeypatch):
    result, operate, step = await run_queue(monkeypatch, tool_proposal("jiuwen_task_cancel", {"job_id": "a"}),
        [{"jobs": [{"job_id": "a", "revision": 1}]}, RuntimeError("connection lost after dispatch")])
    assert result["state"] == "incomplete"
    assert result["error"] == "connection lost after dispatch"
    assert operate.await_count == 2
    assert step.await_count == 1


@pytest.mark.asyncio
async def test_revision_conflict_can_query_then_correct_without_replaying_old_write(monkeypatch):
    def modification(revision):
        return tool_proposal("jiuwen_task_modify", {"job_id": "a", "revision": revision, "instruction": "change"})

    result, operate, _ = await run_queue(monkeypatch, [
        modification(1), tool_proposal("jiuwen_task_query", {}), modification(2), '{"response":"修改已受理。"}',
    ], [
        {"jobs": [{"job_id": "a", "revision": 1}]},
        {"state": "rejected", "applied": False, "error": "revision changed"},
        {"jobs": [{"job_id": "a", "revision": 2}]}, {"state": "accepted"},
    ])
    assert "error" not in result
    assert operate.await_count == 4
    assert operate.await_args_list[1].args[2].arguments["revision"] == 1
    assert operate.await_args_list[3].args[2].arguments["revision"] == 2


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


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal, decisions, queries, error", [
    ("invalid", 4, 1, ""),
    (json.dumps({"tool": {"name": "jiuwen_task_query", "arguments": {}}}), 6, 7, "operation limit"),
])
async def test_management_budgets_are_bounded(monkeypatch, proposal, decisions, queries, error):
    result, operate, step = await run_queue(monkeypatch, proposal, {"jobs": []})
    assert result["state"] == "incomplete"
    assert error in result["error"]
    assert step.await_count == decisions
    assert operate.await_count == queries
