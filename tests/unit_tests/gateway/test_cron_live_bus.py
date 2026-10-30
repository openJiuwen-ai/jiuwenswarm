# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""定时任务执行会话的实时流：晚到的页面还能补上已经产生的结果。"""

from __future__ import annotations

import asyncio

from gateway.cron.live_bus import CronLiveBus


def test_late_subscriber_replays_cron_final() -> None:
    async def _run() -> None:
        bus = CronLiveBus()
        sid = "__cron___1a1640a452f_40c17fbf6426"
        bus.publish(sid, {"event": "chat.final", "payload": {"content": "你好", "session_id": sid}})
        queue = bus.subscribe(sid, replay=True)
        frame = await asyncio.wait_for(queue.get(), timeout=1)
        bus.unsubscribe(sid, queue)
        assert frame["event"] == "chat.final"
        assert frame["payload"]["content"] == "你好"

    asyncio.run(_run())
