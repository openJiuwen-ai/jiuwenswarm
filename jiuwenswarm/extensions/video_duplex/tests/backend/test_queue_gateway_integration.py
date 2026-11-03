import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import queue_manager
from jiuwenswarm.extensions.video_duplex.tests.backend.test_queue_manager import management_call


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
