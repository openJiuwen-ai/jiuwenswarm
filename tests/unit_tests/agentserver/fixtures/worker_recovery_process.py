# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent subprocess fixture using production IPC, dispatcher and journal.

Execution state is deterministic; this validates recovery transport, not a model.
Replay pacing simulates a busy transport without blocking another scope's writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session.model import (
    RuntimeSessionSnapshot, RuntimeSessionState, SessionExecutionHandle,
    SessionExecutionState, SessionPersistencePolicy, SessionWorkKind,
)
from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_SHUTDOWN_DONE
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


def request(session: str) -> AgentRequest:
    return AgentRequest(request_id=f"original-{session}", channel_id="web", user_id="owner",
                        session_id=session, req_method=ReqMethod.CHAT_SEND,
                        params={"session_id": session}, is_stream=True)


class RecoveryRuntime:
    def __init__(self) -> None:
        self.handles = {
            session: SessionExecutionHandle(execution_id=f"execution-{session}", session_id=session,
                                            request_id=f"original-{session}", generation=1,
                                            work_kind=SessionWorkKind.CHAT_STREAM,
                                            state=(SessionExecutionState.WAITING_FOR_CONTROL if session == "b"
                                                   else SessionExecutionState.RUNNING),
                                            waiting_control_id="b-control" if session == "b" else None)
            for session in ("a", "b")
        }

    def attach_gateway_connection(self, _ws, _lock) -> None:
        pass

    def get_session_snapshot(self, session: str) -> RuntimeSessionSnapshot:
        return RuntimeSessionSnapshot(session, "web", SessionPersistencePolicy.PERSISTENT, 1,
                                      RuntimeSessionState.ACTIVE, (self.handles[session].snapshot(),))

    async def dispatch_parsed_request(self, ws, accepted, _lock) -> None:
        await ws.send(json.dumps({"request_id": accepted.request_id, "is_stream": True,
                                  "body": {"event_type": "chat.delta", "content": "live-after-replay"}}))


async def serve(path: str) -> None:
    server = IpcServer(path)
    await server.start()
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    await dispatcher.bind_runtime(RecoveryRuntime())
    await dispatcher.mark_ready()
    bridge.events.register(request("a"), "original-a-dispatch")
    b_record = bridge.events.register(request("b"), "original-b-dispatch")
    b_record.interactions["b-control"] = {"event_type": "chat.ask_user", "interaction_id": "b-control"}
    b_record.is_processing = True
    for index in range(2048):
        bridge.events.append({"request_id": "original-a", "is_stream": True,
                              "body": {"event_type": "chat.delta", "index": index}}, "original-a-dispatch")
    try:
        while not dispatcher.shutdown_requested:
            conn = await server.accept()
            generation = holder.open_session(conn)
            raw_send = conn.send

            async def paced_send(message, raw_send=raw_send):
                if message.get("replayed") and message.get("sequence", 0) % 32 == 1:
                    await asyncio.sleep(0.02)
                await raw_send(message)

            conn.send = paced_send
            await conn.send({"kind": KIND_HELLO, "pid": os.getpid(), "generation": generation,
                             "instance": bridge.events.instance, "runtime_loaded": True, "python_prefix": sys.prefix})
            try:
                while await dispatcher.handle(await conn.recv(), conn):
                    pass
            except (ConnectionError, OSError):
                pass
            if dispatcher.shutdown_requested:
                assert await dispatcher.drain(timeout=2)
                await conn.send({"kind": KIND_SHUTDOWN_DONE, "ok": True})
            await dispatcher.retire_generation(holder.close_session())
            await conn.close()
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1]))
