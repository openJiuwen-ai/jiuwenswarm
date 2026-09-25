# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

from jiuwenswarm.gateway.channel_manager.sdk.streaming import StreamingResponder


class FakeChannelIO:
    """Records sends and edits standing in for a platform API."""

    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []

    async def send(self, text):
        self.sent.append(text)
        return "msg-1"

    async def edit(self, message_id, text):
        assert message_id == "msg-1"
        self.edits.append(text)


async def test_streaming_channel_sends_once_then_edits():
    io = FakeChannelIO()
    responder = StreamingResponder(io.send, io.edit, supports_streaming=True, debounce_ms=1)
    await responder.append("Hel")
    await responder.append("lo")
    await asyncio.sleep(0.05)  # let the debounced flush run
    await responder.append(" world")
    final = await responder.finalize()
    assert final == "Hello world"
    assert io.sent == ["Hello"]              # exactly one message created
    assert io.edits[-1] == "Hello world"     # later content arrives as edits


async def test_non_streaming_channel_sends_single_final_message():
    io = FakeChannelIO()
    responder = StreamingResponder(io.send, io.edit, supports_streaming=False)
    await responder.append("Hello ")
    await responder.append("world")
    assert io.sent == []                     # nothing before finalize
    final = await responder.finalize()
    assert final == "Hello world"
    assert io.sent == ["Hello world"]        # exactly one complete message
    assert io.edits == []


async def test_debounce_limits_edit_frequency():
    io = FakeChannelIO()
    responder = StreamingResponder(io.send, io.edit, supports_streaming=True, debounce_ms=30)
    for token in ("a", "b", "c", "d", "e"):
        await responder.append(token)        # 5 rapid tokens, one pending flush
    await asyncio.sleep(0.06)
    assert len(io.sent) + len(io.edits) <= 2  # far fewer API calls than tokens


async def test_finalize_with_empty_buffer_sends_nothing():
    io = FakeChannelIO()
    responder = StreamingResponder(io.send, io.edit, supports_streaming=True)
    final = await responder.finalize()
    assert final == ""
    assert io.sent == [] and io.edits == []