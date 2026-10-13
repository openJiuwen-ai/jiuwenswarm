# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process protocol stub used by tests and local IPC checks.

The stub speaks the Worker IPC protocol and never imports OpenJiuwen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from jiuwenswarm.server.ipc.protocol import (
    KIND_GATEWAY_ATTACH,
    KIND_HELLO,
    KIND_HEARTBEAT,
    KIND_READINESS,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
    KIND_SHUTDOWN,
    KIND_WIRE,
    PROTOCOL_VERSION,
)
from jiuwenswarm.server.ipc.server import IpcServer

logger = logging.getLogger(__name__)


class StubExit(Exception):
    """Ask the Worker entrypoint to terminate this stub process."""


async def serve_stub(
    socket_path: str,
    *,
    ready_delay: float = 0.0,
    exit_on_request: bool = False,
) -> None:
    server = IpcServer(socket_path)
    await server.start()
    logger.info("[Worker] stub listening: %s", socket_path)
    generation = 0
    runtime_loaded = False
    instance = uuid.uuid4().hex
    try:
        while True:
            conn = await server.accept()
            generation += 1
            try:
                closed = await _serve_one(
                    conn,
                    generation=generation,
                    ready_delay=ready_delay,
                    exit_on_request=exit_on_request,
                    runtime_loaded=runtime_loaded,
                    instance=instance,
                )
            except (OSError, asyncio.IncompleteReadError):
                runtime_loaded = True
                continue
            runtime_loaded = True
            if closed:
                return
    finally:
        await server.close()


async def _serve_one(
    conn: object,
    *,
    generation: int,
    ready_delay: float,
    exit_on_request: bool,
    runtime_loaded: bool,
    instance: str,
) -> bool:
    send = getattr(conn, "send")
    recv = getattr(conn, "recv")
    await send(
        {
            "kind": KIND_HELLO,
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "instance": instance,
            "stub": True,
            "runtime_loaded": runtime_loaded,
            "generation": generation,
        }
    )
    if ready_delay > 0:
        await asyncio.sleep(ready_delay)
    await send({"kind": KIND_READINESS, "state": "AGENT_READY", "reason": ""})
    while True:
        message = await recv()
        kind = str(message.get("kind") or "")
        if kind == KIND_SHUTDOWN:
            return True
        if kind == KIND_HEARTBEAT:
            await send({"kind": KIND_HEARTBEAT, "pid": os.getpid()})
            continue
        if kind == KIND_GATEWAY_ATTACH:
            continue
        if kind == KIND_REQUEST:
            incoming = message.get("generation")
            if isinstance(incoming, int) and incoming != generation:
                continue
            if exit_on_request:
                raise StubExit("stub exit on request")
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            request_id = str(payload.get("request_id") or message.get("request_id") or "")
            frame_generation = incoming if isinstance(incoming, int) else generation
            await send(
                {
                    "kind": KIND_WIRE,
                    "request_id": request_id,
                    "generation": frame_generation,
                    "payload": {
                        "request_id": request_id,
                        "echo": payload.get("req_method"),
                        "session_id": payload.get("session_id"),
                    },
                }
            )
            await send(
                {
                    "kind": KIND_REQUEST_DONE,
                    "request_id": request_id,
                    "generation": frame_generation,
                    "ok": True,
                    "delivered": True,
                }
            )
