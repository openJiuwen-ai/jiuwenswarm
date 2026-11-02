from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelEvent, ChannelManager


class _DummyMessageHandler:
    async def handle_message(self, msg: Any) -> None:  # pragma: no cover
        raise AssertionError("not used in this test")


class _RecordingSubscriber:
    def __init__(self, fail_first: bool = False) -> None:
        self.events: list[ChannelEvent] = []
        self._fail_first = fail_first

    async def __call__(self, event: ChannelEvent) -> None:
        self.events.append(event)
        if self._fail_first and len(self.events) == 1:
            raise RuntimeError("boom")


async def _drain(cm: ChannelManager) -> None:
    await asyncio.gather(*cm._channel_event_tasks)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_report_channel_event_dispatches_to_subscribers() -> None:
    cm = ChannelManager(_DummyMessageHandler())
    sub = _RecordingSubscriber()
    cm.subscribe_channel_events(sub)

    cm.report_channel_event("connected", "tui", "u1")
    await _drain(cm)

    assert [(e.event_type, e.channel_type, e.user_id) for e in sub.events] == [
        ("connected", "tui", "u1")
    ]
    assert not cm._channel_event_tasks  # 完成后引用已清理


@pytest.mark.asyncio
async def test_report_channel_event_swallows_callback_error() -> None:
    cm = ChannelManager(_DummyMessageHandler())
    sub = _RecordingSubscriber(fail_first=True)
    cm.subscribe_channel_events(sub)

    cm.report_channel_event("connected", "tui", "u1")
    await _drain(cm)
    cm.report_channel_event("disconnected", "tui", "u1")
    await _drain(cm)

    # 回调异常被兜底记录，不影响订阅与后续事件分发
    assert [e.event_type for e in sub.events] == ["connected", "disconnected"]
