import asyncio
import json
from typing import Any

import pytest
from websockets.legacy.client import connect as legacy_connect
from websockets.legacy.server import serve as legacy_serve


async def _run_keepalive_backpressure_test(
    *,
    client_max_queue: int | None,
    expect_keepalive_timeout: bool,
) -> None:
    server_sent: list[Any] = []

    async def handler(ws: Any) -> None:
        for i in range(3):
            await ws.send(json.dumps({"seq": i}))
        try:
            async for _raw in ws:
                pass
        except Exception:
            pass
        server_sent.append(getattr(ws, "close_sent", None))

    server = await legacy_serve(
        handler,
        "127.0.0.1",
        0,
        ping_interval=0.5,
        ping_timeout=1.5,
    )
    port = server.sockets[0].getsockname()[1]

    try:
        ws = await legacy_connect(
            f"ws://127.0.0.1:{port}",
            ping_interval=None,
            max_queue=client_max_queue,
        )
        await asyncio.sleep(4)
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        server.close()
        await server.wait_closed()

    assert len(server_sent) == 1, "handler should have recorded close_sent"
    close = server_sent[0]
    if expect_keepalive_timeout:
        assert close is not None, "server should have sent a close frame"
        assert close.code == 1011, f"expected 1011 keepalive timeout, got {close.code}"
        assert "keepalive ping timeout" in (close.reason or ""), (
            f"expected 'keepalive ping timeout' in reason, got {close.reason!r}"
        )
    else:
        if close is not None:
            assert close.code != 1011, (
                f"should NOT be keepalive timeout, got {close.code}/{close.reason}"
            )


@pytest.mark.asyncio
@pytest.mark.slow
async def test_keepalive_timeout_when_client_recv_queue_full() -> None:
    """验证 client recv 队列满(max_queue=2)时 transfer_data backpressure 阻塞 ping/pong，
    导致 server keepalive ping timeout 关连接。"""
    await _run_keepalive_backpressure_test(
        client_max_queue=2,
        expect_keepalive_timeout=True,
    )


@pytest.mark.asyncio
@pytest.mark.slow
async def test_keepalive_ok_when_client_recv_queue_unbounded() -> None:
    """对照：max_queue=None 时即使不调 recv()，transfer_data 不阻塞，ping/pong 正常。"""
    await _run_keepalive_backpressure_test(
        client_max_queue=None,
        expect_keepalive_timeout=False,
    )
