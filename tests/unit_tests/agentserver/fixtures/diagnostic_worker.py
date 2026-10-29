# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fault injection using the production Worker service/dispatcher and OS IPC.

Only Agent Runtime construction is replaced with a deterministic traffic
producer. No model or Team Code acceptance is claimed by this fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from jiuwenswarm.server.ipc.protocol import KIND_REQUEST
from jiuwenswarm.server.worker import diagnostics, service
from jiuwenswarm.server.worker.main import main


class SlowShellProbe:
    """Keep one discovery call pending even during normal Worker shutdown."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = 0
        self.threads: set[int] = set()
        self.daemon = True
        self.started = 0.0
        self.finished = False

    def __call__(self) -> str:
        with self.lock:
            self.calls += 1
            self.threads.add(threading.get_ident())
            self.daemon = self.daemon and threading.current_thread().daemon
            self.started = self.started or time.monotonic()
        # This owned child's injected OS probe deliberately remains pending
        # when the real Worker receives shutdown. A non-daemon implementation
        # would therefore fail the parent's graceful-exit assertion.
        self.release.wait(120)
        self.finished = True
        return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "probe_calls": self.calls,
                "probe_threads": len(self.threads),
                "probe_daemon": self.daemon,
                "probe_pending": bool(self.started) and not self.finished,
                "probe_seconds": time.monotonic() - self.started
                if self.started
                else 0.0,
            }


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
        if mode == "slow-shell":
            from openjiuwen.harness.prompts import SystemPromptBuilder

            from jiuwenswarm.agents.harness.common.prompt import shell_environment
            from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import (
                RuntimePromptRail,
            )

            self.shell_probe = SlowShellProbe()
            shell_environment._available_powershell = self.shell_probe
            self.shell_rails = []

            async def runtime_state(_ctx):
                # This fixture has no Agent Runtime/configuration or model.
                # Isolate unrelated attachment IO, retaining the real hook.
                return {"channel": "diagnostic"}

            for _ in range(12):
                rail = RuntimePromptRail(language="en", channel="diagnostic")
                rail.system_prompt_builder = SystemPromptBuilder(language="en")
                rail._refresh_dynamic_attachments = runtime_state
                self.shell_rails.append(rail)
        if mode == "slow-logging":
            from jiuwenswarm.common import utils  # noqa: F401
            from jiuwenswarm.common.background_logging import wrap_log_handlers

            self.log_started = threading.Event()
            self.log_release = threading.Event()
            self.log_thread = None
            runtime = self

            class SlowHandler(logging.Handler):
                def emit(self, record):
                    runtime.log_thread = threading.current_thread().name
                    runtime.log_started.set()
                    runtime.log_release.wait(120)

            self.slow_logger = logging.getLogger("slow-disk-injection")
            self.slow_logger.handlers = [SlowHandler()]
            self.slow_logger.propagate = False
            self.slow_logger.setLevel(logging.INFO)
            wrap_log_handlers(self.slow_logger)
        if mode == "regex-logging":
            from jiuwenswarm.common import utils  # noqa: F401

            self.utils_origin = utils.__file__

    async def attach_gateway_connection(self, ws, send_lock) -> None:
        pass

    async def on_gateway_disconnect(self, ws, remote) -> None:
        pass

    async def shutdown(self) -> None:
        self.release.set()

    async def dispatch_parsed_request(self, ws, request, send_lock) -> None:
        if self.mode == "regex-logging":
            await ws.send(json.dumps({
                "request_id": request.request_id, "phase": "regex_starting",
                "utils_origin": self.utils_origin,
            }))
            started = time.monotonic()
            logging.getLogger("regex-injection").info("payload=%s", "a+" * 100_000)
            await ws.send(json.dumps({
                "request_id": request.request_id, "phase": "final",
                "log_seconds": time.monotonic() - started,
            }))
            return
        if self.mode == "slow-logging":
            await self._slow_logging_request(ws, request)
            return
        if self.mode == "slow-shell":
            await self._slow_shell_request(ws, request)
            return
        if self.mode == "freeze" and request.params.get("freeze", True):
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

    async def _slow_logging_request(self, ws, request) -> None:
        started = time.monotonic()
        self.slow_logger.info("injected disk write: api_key=test-only-secret")
        async with asyncio.timeout(5):
            while not self.log_started.is_set():
                await asyncio.sleep(0.01)
        await ws.send(json.dumps({
            "request_id": request.request_id, "phase": "logging_pending",
            "first_seconds": time.monotonic() - started, "log_thread": self.log_thread,
        }))
        # Stay beyond the real 30s transport health contract. The log sink
        # deliberately remains blocked through final Worker shutdown too.
        await asyncio.sleep(36)
        await ws.send(json.dumps({
            "request_id": request.request_id, "phase": "final",
            "blocked_seconds": time.monotonic() - started,
            "sink_pending": not self.log_release.is_set(),
        }))

    async def _slow_shell_request(self, ws, request) -> None:
        ctx = SimpleNamespace(agent=None, session=None)
        started = time.monotonic()

        async def round_trip():
            await asyncio.gather(
                *(rail.before_model_call(ctx) for rail in self.shell_rails)
            )
            return all(
                "not yet verified" in rail.system_prompt_builder.build()
                for rail in self.shell_rails
            )

        unknown = await round_trip()
        first_round_seconds = time.monotonic() - started
        await ws.send(
            json.dumps(
                {
                    "request_id": request.request_id,
                    "phase": "shell_pending",
                    "unknown": unknown,
                    "first_round_seconds": first_round_seconds,
                    **self.shell_probe.snapshot(),
                }
            )
        )
        rounds = 1
        while time.monotonic() - started < 36:
            await asyncio.sleep(1)
            unknown = await round_trip() and unknown
            rounds += 1
        await ws.send(
            json.dumps(
                {
                    "request_id": request.request_id,
                    "phase": "final",
                    "unknown": unknown,
                    "rails": len(self.shell_rails),
                    "rounds": rounds,
                    **self.shell_probe.snapshot(),
                }
            )
        )


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["pool-pressure", "freeze", "crash", "slow-shell", "slow-logging", "regex-logging"], required=True
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
