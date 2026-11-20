# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic business output with real Runtime adoption and durable ownership."""

from __future__ import annotations

import json
import os

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
from jiuwenswarm.server.runtime.session.lifecycle import release_runtime
from jiuwenswarm.server.worker import service


class OwnershipRuntime:
    def __init__(self) -> None:
        # Exercise the actual persisted-session adoption path and coordinator,
        # without constructing a model, AgentManager or external providers.
        self.runtime = AgentRuntime.__new__(AgentRuntime)
        self.runtime._closed = False
        self.runtime._session_coordinator = RuntimeSessionCoordinator()
        self.sessions: set[str] = set()

    async def attach_gateway_connection(self, _ws, _lock) -> None:
        pass

    async def on_gateway_disconnect(self, _ws, _remote) -> None:
        pass

    async def dispatch_parsed_request(self, ws, request, _lock) -> None:
        await self.runtime._ensure_session_registered(request)
        self.sessions.add(request.session_id)
        path = get_agent_sessions_dir() / request.session_id / "history.jsonl"
        before = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        turn = {"request_id": request.request_id, "content": request.params["content"]}
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(turn) + "\n")
        await ws.send(json.dumps({
            "request_id": request.request_id, "session_id": request.session_id,
            "phase": "completed", "pid": os.getpid(), "history_before": before,
            "content": turn["content"],
        }))

    async def shutdown(self) -> None:
        for session_id in self.sessions:
            release_runtime(session_id)


def configure() -> None:
    async def load_runtime(_host, _port, signals, dispatcher, loaded):
        runtime = OwnershipRuntime()
        await dispatcher.bind_runtime(runtime)
        loaded["runtime"] = runtime
        signals.agent_ready()
        return runtime

    service._load_runtime = load_runtime
