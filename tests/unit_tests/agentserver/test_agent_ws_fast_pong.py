# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""_FastPongServerProtocol 回归测试：pong 必须绕过 drain 直写 transport。

背景（OA.05000010，2026-10-26 生产事故）：Gateway 侧 30s ping / 10s pong 超时 /
连续 8 次 miss 即主动断链。默认 ``WebSocketServerProtocol.pong()`` 走
``write_frame()`` = ``write_frame_sync()`` + ``await drain()``——出站缓冲积压时
读循环停在 pong 的 drain 上，后续 ping 无人读取，最终被网关判死。
修复后 pong 路径纯同步（同帧直写 transport），读循环永不停在 pong 上。
"""

import asyncio

import pytest

from websockets.protocol import State


class _FakeTransport:
    """记录 write() 字节，不提供任何流控语义。"""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data) -> None:
        self.frames.append(bytes(data))


def _make_protocol(cls):
    """__new__ 轻量构造：只填 write_frame_sync 需要的最小字段，绕开握手。"""
    protocol = cls.__new__(cls)
    protocol.state = State.OPEN
    protocol.transport = _FakeTransport()
    protocol.is_client = False
    protocol.debug = False
    protocol.extensions = []
    return protocol


async def _never_returns() -> None:
    """挂死的 drain：任何 ``await drain()`` 的路径都无法按时返回。"""
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_pong_writes_frame_without_awaiting_drain() -> None:
    mod = pytest.importorskip("jiuwenswarm.server.agent_ws_server")
    protocol = _make_protocol(mod._FastPongServerProtocol)
    # drain 永不返回：若 pong 仍走 write_frame() → await drain()，
    # wait_for 必超时——这正是 10-26 故障里 pong 被出站积压扣住的形态。
    protocol.drain = _never_returns

    await asyncio.wait_for(protocol.pong(b"hb"), timeout=1.0)

    # pong 帧已同步写入 transport：FIN=1 + OP_PONG(0x0A) → 0x8A，len=2，无 mask。
    assert protocol.transport.frames == [b"\x8a\x02hb"]


@pytest.mark.asyncio
async def test_pong_skips_when_connection_not_open() -> None:
    mod = pytest.importorskip("jiuwenswarm.server.agent_ws_server")
    protocol = _make_protocol(mod._FastPongServerProtocol)
    protocol.state = State.CLOSED

    await asyncio.wait_for(protocol.pong(b"hb"), timeout=1.0)

    assert protocol.transport.frames == []


@pytest.mark.asyncio
async def test_start_injects_fast_pong_protocol_into_serve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    mod = pytest.importorskip("jiuwenswarm.server.agent_ws_server")
    AgentWebSocketServer = mod.AgentWebSocketServer

    captured: dict = {}

    class _ServeCaptured(Exception):
        """让 start() 在 serve 调用点立即中断，跳过 warmup/sandbox 等后续步骤。"""

    async def _fake_legacy_serve(handler, host, port, **kwargs):
        captured.update(kwargs)
        raise _ServeCaptured

    monkeypatch.setattr("websockets.legacy.server.serve", _fake_legacy_serve)
    # start() 在 serve 之前会触碰的模块级依赖全部打桩，避免真实配置/状态副作用。
    monkeypatch.setattr(mod, "get_config", lambda: {"startup": {"warmup": {"enabled": False}}})
    monkeypatch.setattr(mod, "reset_harness_packages_state", lambda: None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization."
        "file_store.configure_shared_skill_authorization",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        lambda: tmp_path / "skills",
    )

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._server = None
    server._host = "127.0.0.1"
    server._port = 18000
    server._ping_interval = 30
    server._ping_timeout = 10
    server._connection_handler = lambda _ws: None
    server._process_request = lambda _path, _headers: None

    with pytest.raises(_ServeCaptured):
        await server.start(listen=True)

    assert captured["create_protocol"] is mod._FastPongServerProtocol
