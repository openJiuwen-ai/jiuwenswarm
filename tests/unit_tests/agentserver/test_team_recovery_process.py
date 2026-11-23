# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real process replacement restores a persisted SDK Team without chat.send."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import uuid

import psutil
import pytest

from jiuwenswarm.common import paths, utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["process-exit", "heartbeat-stall"])
async def test_original_team_automatically_continues_after_worker_replacement(monkeypatch, tmp_path, failure):
    from jiuwenswarm.server.supervisor import runtime_supervisor as supervisor_module

    data = tmp_path / "data"
    allow_second = threading.Event()
    pending_second = threading.Event()
    team_built = threading.Event()
    requests = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            is_leader = "CONTROLLED LEADER" in json.dumps(body["messages"])
            recorded = effects.read_text(encoding="utf-8") if effects.exists() else ""
            function = next((tool["function"]["name"] for tool in body.get("tools", [])
                             if "durable_step" in tool["function"]["name"]), None)
            delta = {"role": "assistant", "content": "Standing by"}
            build = next((tool["function"]["name"] for tool in body.get("tools", [])
                          if tool["function"]["name"] == "build_team"), None)
            if is_leader and build and not team_built.is_set():
                team_built.set()
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "build-original", "type": "function",
                    "function": {"name": build, "arguments": json.dumps({
                        "display_name": "Recovery test", "desc": "Execute both durable steps",
                        "leader_display_name": "Leader", "leader_desc": "Controlled test leader",
                    })}}]}
            elif is_leader and function and '"step": 2' not in recorded:
                number = 2 if '"step": 1' in recorded else 1
                if number == 2:
                    pending_second.set()
                    allow_second.wait(90)
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"step-{number}", "type": "function",
                    "function": {"name": function, "arguments": json.dumps({"number": number})}}]}
            elif is_leader:
                delta = {"role": "assistant", "content": "Both steps complete"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            finish = "tool_calls" if "tool_calls" in delta else "stop"
            frames = [
                {"id": "controlled", "object": "chat.completion.chunk", "model": "controlled", "created": 1,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "controlled", "object": "chat.completion.chunk", "model": "controlled", "created": 1,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            try:
                for frame in frames:
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import sys\nif 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from automatic_team_worker import configure\n    configure()\n", encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([
        str(bootstrap), str(Path(__file__).parent / "fixtures"), str(Path(__file__).resolve().parents[3]),
    ]))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    monkeypatch.setattr(paths, "_workspace_base_dir", data)
    monkeypatch.setattr(paths, "_initialized", False)
    for name in ("_config_dir", "_workspace_dir", "_root_dir"):
        monkeypatch.setattr(paths, name, None)
    effects = utils.get_agent_root_dir() / "effects.jsonl"
    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "automatic-team-process-test")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("TEAM_TEST_MODEL", f"http://127.0.0.1:{model.server_port}/v1")
    endpoint = str(tmp_path / "worker.pipe") if os.name == "nt" else f"/tmp/jw-team-{uuid.uuid4().hex[:8]}.sock"
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, forwarder=EventForwarder(), socket_path=endpoint)

    class Gateway:
        async def send(self, wire):
            pass

    async def until(predicate, seconds=90):
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(.05)

    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 45)
        assert await readiness.wait_agent_ready(90)
        old_pid = supervisor.worker_pid
        request = AgentRequest(request_id="only-user-request", channel_id="web", session_id="original",
                               req_method=ReqMethod.CHAT_SEND, params={"content": "Execute both durable steps"})
        await supervisor.dispatch_parsed_request(Gateway(), request, asyncio.Lock())
        await until(pending_second.is_set)
        assert [json.loads(line)["step"] for line in effects.read_text().splitlines()] == [1]
        before_replacement = len(requests)
        if failure == "process-exit":
            psutil.Process(old_pid).kill()
        else:
            # Exercise the production watchdog replacement path, not just kill.
            import time
            supervisor._worker_last_beat = time.monotonic() - supervisor_module._WORKER_STALL_RESTART - 1
            assert await supervisor._classify_worker_stall(time.monotonic())
        await until(lambda: supervisor.worker_pid != old_pid and readiness.snapshot()["agent_ready"])
        allow_second.set()
        await until(lambda: effects.exists() and '"step": 2' in effects.read_text())
        result = [json.loads(line) for line in effects.read_text().splitlines()]
        assert [item["step"] for item in result] == [1, 2]
        assert result[0]["pid"] == old_pid and result[1]["pid"] == supervisor.worker_pid
        assert not psutil.pid_exists(old_pid)
        recovered_requests = list(requests)[before_replacement:]
        (tmp_path / "recovered-model-requests.json").write_text(json.dumps(recovered_requests, indent=2), encoding="utf-8")
        assert any("Execute both durable steps" in json.dumps(body["messages"]) for body in recovered_requests)
        assert any("Runtime recovery notice" in json.dumps(body["messages"]) for body in recovered_requests)
        # No second frontend request was submitted. The durable Team identity
        # and checkpoint were consumed by the SDK's no-input start path.
        record_path = paths.get_lifecycle_dir() / "resources" / "session_original.json"
        record = json.loads(record_path.read_text())["team_recovery"]
        assert record["recipe"]["team_name"] == "recovery-team"
        assert record["attempts"] == 1
        assert "query" not in record["recipe"]
    finally:
        allow_second.set()
        await supervisor.stop(terminate_worker=True)
        await asyncio.to_thread(model.shutdown)
        model.server_close()
