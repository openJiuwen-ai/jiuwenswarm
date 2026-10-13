# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fault injection using the production Worker service/dispatcher and OS IPC.

Only Agent Runtime construction is replaced with a deterministic traffic
producer. No model or Team Code acceptance is claimed by this fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from jiuwenswarm.server.ipc.protocol import KIND_REQUEST
from jiuwenswarm.server.worker import diagnostics, service
from jiuwenswarm.server.worker.main import main


class TrafficRuntime:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.release = threading.Event()
        self.pool = None
        if mode == "pool-pressure":
            self.pool = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="injected-busy-runtime"
            )
            asyncio.get_running_loop().set_default_executor(self.pool)
            for _ in range(2):
                self.pool.submit(self.release.wait)

    def attach_gateway_connection(self, ws, send_lock) -> None:
        pass

    async def on_gateway_disconnect(self, ws, remote) -> None:
        pass

    async def shutdown(self) -> None:
        self.release.set()

    async def dispatch_parsed_request(self, ws, request, send_lock) -> None:
        if self.mode == "freeze":
            await ws.send(
                json.dumps({"request_id": request.request_id, "phase": "freezing"})
            )
            time.sleep(60)  # Intentional event-loop stall, in this owned child only.
            return
        frames = int(request.params.get("frames", 64))
        size = int(request.params.get("bytes", 8192))
        for sequence in range(frames):
            await ws.send(
                json.dumps(
                    {
                        "request_id": request.request_id,
                        "sequence": sequence,
                        "data": "x" * size,
                    }
                )
            )
            await asyncio.sleep(0.001)
        await ws.send(json.dumps({"request_id": request.request_id, "phase": "final"}))


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["pool-pressure", "freeze", "crash"], required=True
    )
    args, remaining = parser.parse_known_args()

    async def load_runtime(host, port, signals, dispatcher, loaded):
        runtime = TrafficRuntime(args.mode)
        await dispatcher.bind_runtime(runtime)
        loaded["runtime"] = runtime
        signals.agent_ready()
        return runtime

    service._load_runtime = load_runtime
    if args.mode == "crash":

        async def crashing_session(conn, dispatcher):
            diagnostics.arm_stall_dump()
            try:
                while True:
                    message = await conn.recv()
                    if message.get("kind") == KIND_REQUEST:
                        raise service.WorkerForceExit("injected diagnostic exit")
                    if not await dispatcher.handle(message, conn):
                        return False
            finally:
                diagnostics.cancel_stall_dump()

        service._read_session = crashing_session
    sys.argv = [sys.argv[0], *remaining]
    main()


if __name__ == "__main__":
    run()
