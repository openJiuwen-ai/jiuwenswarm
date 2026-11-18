"""企业版团队直播：补写历史，取消时丢掉还没发出的帧。"""
from __future__ import annotations

import asyncio

from jiuwenswarm.enterprise.team import session_live as live


def _records(monkeypatch):
    written: list[dict] = []

    def append_history_record(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        append_history_record,
    )
    return written


def test_live_history_flushes_deltas_on_empty_final(monkeypatch):
    written = _records(monkeypatch)
    history = live._LiveHistory(session_id="web_1", channel_id="web")
    history.observe({"event_type": "chat.delta", "content": "你"})
    history.observe({"event_type": "chat.delta", "content": "好"})
    history.observe({"event_type": "chat.notice", "content": "已有通知"})
    history.observe({"event_type": "team.member", "event": {"type": "team.member.spawned"}})
    history.observe({"event_type": "team.task", "event": {"task_id": "t1"}})
    history.observe({"event_type": "chat.final", "content": ""})

    assert len(written) == 1
    assert written[0]["event_type"] == "chat.final"
    assert written[0]["content"] == "你好"
    assert written[0]["mode"] == "team"
    assert written[0]["role"] == "assistant"


def test_live_history_keeps_final_text_and_file(monkeypatch):
    written = _records(monkeypatch)
    history = live._LiveHistory(session_id="web_1", channel_id="web")
    history.observe({"event_type": "chat.delta", "content": "草稿"})
    history.observe({"event_type": "chat.final", "content": "定稿"})
    history.observe({
        "event_type": "chat.file",
        "content": "",
        "files": [{"name": "a.py"}],
    })
    history.observe({
        "event_type": "chat.tool_call",
        "content": "",
        "tool_call": {"name": "write"},
    })

    assert [item["event_type"] for item in written] == [
        "chat.final",
        "chat.file",
        "chat.tool_call",
    ]
    assert written[0]["content"] == "定稿"
    assert written[1]["extra"]["files"] == [{"name": "a.py"}]
    assert written[2]["extra"]["tool_call"] == {"name": "write"}


def test_live_history_tool_call_flushes_pending_text(monkeypatch):
    written = _records(monkeypatch)
    history = live._LiveHistory(session_id="web_1", channel_id=None)
    history.observe({"event_type": "chat.delta", "content": "先说"})
    history.observe({
        "event_type": "chat.ask_user_question",
        "content": "",
        "questions": [{"question": "继续吗"}],
    })

    assert [item["event_type"] for item in written] == [
        "chat.final",
        "chat.ask_user_question",
    ]
    assert written[0]["content"] == "先说"
    assert written[0]["channel_id"] == "default"


class _HoldQueue(asyncio.Queue):
    def __init__(self) -> None:
        super().__init__()
        self.hold = asyncio.Event()

    async def get(self):
        await self.hold.wait()
        return await super().get()


class _TeamManager:
    def has_stream_task(self, session_id: str) -> bool:
        return True

    def remove_waiter(self, session_id: str, request_id: str) -> None:
        return None


def test_drop_queued_does_not_publish(monkeypatch):
    published: list[object] = []
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.team.push_fanout.publish",
        lambda wire: published.append(wire),
    )

    async def run() -> None:
        queue = _HoldQueue()
        queue.put_nowait({"event_type": "chat.delta", "content": "晚到"})
        task = asyncio.create_task(
            live._forward_session_live(_TeamManager(), "web_1", "web", queue, "rid")
        )
        live._lives["web_1"] = live._SessionLive(queue=queue, task=task)
        await asyncio.sleep(0)
        await live.stop_team_session_live("web_1", drop_queued=True)
        queue.hold.set()
        await task

    try:
        asyncio.run(run())
    finally:
        live._lives.pop("web_1", None)
    assert published == []
    assert "web_1" not in live._lives
