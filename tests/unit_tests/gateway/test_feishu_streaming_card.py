import asyncio

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_streaming_card import (
    FeishuStreamingSession,
)


class _RecordingCardKit:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.closed: list[str] = []

    async def create_card(self) -> str:
        return "card-1"

    async def update_content(self, _card_id: str, content: str, _sequence: int) -> None:
        self.updates.append(content)

    async def close_card(self, _card_id: str, summary: str, _sequence: int) -> None:
        self.closed.append(summary)


async def _send_card(_content: str) -> None:
    return None


@pytest.mark.asyncio
async def test_flushes_text_added_while_card_update_is_in_flight():
    """A delta received during an update must schedule a follow-up update."""
    written: list[str] = []
    first_update_started = asyncio.Event()
    session: FeishuStreamingSession

    class FakeCardKit:
        async def create_card(self) -> str:
            return "card-1"

        async def update_content(self, _card_id: str, content: str, _sequence: int) -> None:
            written.append(content)
            if content == "first":
                session.replace("second")
                first_update_started.set()

        async def close_card(self, _card_id: str, _summary: str, _sequence: int) -> None:
            return None

    async def send_card(_content: str) -> None:
        return None

    session = FeishuStreamingSession(FakeCardKit(), send_card, debounce_ms=0)
    await session.start()
    session.replace("first")

    await first_update_started.wait()
    for _ in range(5):
        await asyncio.sleep(0)

    assert written == ["first", "second"]


@pytest.mark.asyncio
async def test_finalize_skips_empty_snapshot_but_closes_card():
    """无文字产出的回合：不能向飞书推空 content（400 field validation failed），
    但必须 close_card 退出流式状态，否则卡片永远停在"正在生成回复…"。"""
    cardkit = _RecordingCardKit()
    session = FeishuStreamingSession(cardkit, _send_card, debounce_ms=0)
    await session.start()

    await session.finalize("")

    assert cardkit.updates == []
    assert len(cardkit.closed) == 1


@pytest.mark.asyncio
async def test_finalize_with_text_writes_snapshot_then_closes():
    cardkit = _RecordingCardKit()
    session = FeishuStreamingSession(cardkit, _send_card, debounce_ms=0)
    await session.start()

    await session.finalize("最终回复")

    assert cardkit.updates == ["最终回复"]
    assert cardkit.closed == ["最终回复"]


@pytest.mark.asyncio
async def test_empty_delta_does_not_hit_update_content():
    """flush 空快照同样要跳过，避免 400。"""
    cardkit = _RecordingCardKit()
    session = FeishuStreamingSession(cardkit, _send_card, debounce_ms=0)
    await session.start()

    session.replace("")
    await asyncio.sleep(0.05)

    assert cardkit.updates == []
