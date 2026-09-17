import asyncio

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_streaming_card import (
    FeishuStreamingSession,
)


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
