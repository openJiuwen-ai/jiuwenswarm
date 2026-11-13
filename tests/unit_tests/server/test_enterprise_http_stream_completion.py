"""Enterprise HTTP EOF must follow Runtime cleanup, not a complete chunk."""

import asyncio
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.http_invoke import invoke_stream
from jiuwenswarm.server.transports import http_sink


@pytest.mark.asyncio
@pytest.mark.parametrize("enterprise", [True, False])
async def test_complete_chunk_preserves_enterprise_cleanup_and_personal_eof(monkeypatch, enterprise):
    monkeypatch.setattr(http_sink, "is_enterprise", lambda: enterprise)
    sink = http_sink.SSESink()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def handler(ctx):
        await ctx.sink.send_chunk(AgentResponseChunk(
            request_id="root", channel_id="web", payload=None, is_complete=True,
        ), sequence=1)
        await release_cleanup.wait()
        cleanup_finished.set()

    ctx = SimpleNamespace(request=AgentRequest(request_id="root", channel_id="web"),
                          sink=sink, services=SimpleNamespace())
    task = asyncio.create_task(invoke_stream(ctx, handler))
    tail = None
    try:
        async with aclosing(sink.frames()) as frames:
            assert '"is_complete": true' in await anext(frames)
            tail = asyncio.create_task(anext(frames, None))
            await asyncio.sleep(0)
            if enterprise:
                assert not tail.done(), "HTTP EOF must wait for Runtime control bookkeeping"
                assert not cleanup_finished.is_set()
            else:
                assert tail.done() and tail.result() is None
            release_cleanup.set()
            await task
            assert await tail is None
            assert cleanup_finished.is_set()
    finally:
        release_cleanup.set()
        await asyncio.gather(task, return_exceptions=True)
        if tail is not None:
            await asyncio.gather(tail, return_exceptions=True)
