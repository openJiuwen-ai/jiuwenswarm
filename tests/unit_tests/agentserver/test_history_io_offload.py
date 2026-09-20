import asyncio
import threading

import pytest

from jiuwenswarm.server.runtime.session.history_io import (
    run_history_io as _run_history_io, run_stream_parser,
)


@pytest.mark.asyncio
async def test_history_backpressure_does_not_block_event_loop():
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    def write():
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return "written"
    task = asyncio.create_task(_run_history_io(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # An unrelated request must finish before the blocked write is released.
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.5)
        assert not task.done()
    finally:
        release.set()
    assert await task == "written"


@pytest.mark.asyncio
async def test_cancellation_drains_running_write_before_next_boundary():
    entered, release = threading.Event(), threading.Event()
    records = []
    def write():
        entered.set()
        assert release.wait(3)
        records.append("first")
    task = asyncio.create_task(_run_history_io(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _run_history_io(records.append, "cleanup")
    assert records == ["first", "cleanup"]


@pytest.mark.asyncio
async def test_subagent_parser_persistence_runs_off_loop(monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    loop_thread = threading.get_ident()
    recorded = []
    def append(**kwargs):
        assert threading.get_ident() != loop_thread
        recorded.append(kwargs)
    monkeypatch.setattr(interface_deep, "append_history_record", append)
    chunk = SimpleNamespace(type="subagent_message", payload={"subagent_message": {
        "parent_session_id": "parent", "subagent_id": "child", "seq": 1,
        "content": "done", "role": "assistant", "event_type": "chat.final",
    }})
    await run_stream_parser(interface_deep.JiuWenSwarmDeepAdapter._parse_stream_chunk, chunk)
    assert len(recorded) == 1
    assert recorded[0]["subagent_id"] == "child"


@pytest.mark.asyncio
async def test_plain_token_parser_does_not_dispatch_a_thread():
    from types import SimpleNamespace
    loop_thread = threading.get_ident()
    def parse(chunk):
        assert threading.get_ident() == loop_thread
        return chunk.payload
    assert await run_stream_parser(parse, SimpleNamespace(type="llm_output", payload="hello")) == "hello"
