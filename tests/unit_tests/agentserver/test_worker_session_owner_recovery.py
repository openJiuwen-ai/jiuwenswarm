# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An existing Session must execute again after its owning Worker is replaced."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import psutil
import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common import utils
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.ipc.client import endpoint_is_listening
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


class Gateway:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def send(self, wire):
        await self.messages.put(json.loads(wire))


async def execute(supervisor, content, *, expect_error=False):
    gateway = Gateway()
    lock = asyncio.Lock()
    supervisor.attach_gateway_connection(gateway, lock)
    request = AgentRequest(
        request_id=uuid.uuid4().hex, channel_id="web", session_id="persisted",
        req_method=ReqMethod.CHAT_SEND, params={"content": content},
    )
    await supervisor.dispatch_parsed_request(gateway, request, lock)
    async with asyncio.timeout(15):
        while True:
            message = await gateway.messages.get()
            if message.get("request_id") == request.request_id:
                if expect_error and message.get("response_kind") == "e2a.error":
                    return message
                assert message.get("response_kind") != "e2a.error", message
                if message.get("phase") == "completed":
                    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    "different-build", "exited-and-reaped", "live-owner",
    pytest.param("unreaped-zombie", marks=pytest.mark.skipif(
        sys.platform != "linux", reason="requires the Linux kernel's unreaped zombie state",
    )),
])
async def test_replacement_worker_reclaims_and_executes_persisted_session(monkeypatch, tmp_path, replacement):
    root = Path(__file__).resolve().parents[3]
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from session_owner_worker import configure\n"
        "    configure()\n", encoding="utf-8",
    )
    paths = [str(bootstrap), str(Path(__file__).parent / "fixtures"), str(root)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    data = tmp_path / "data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    monkeypatch.setattr(utils, "_workspace_base_dir", data)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "owner-recovery-old-build")
    session = data / "agent" / "sessions" / "persisted"
    session.mkdir(parents=True)
    metadata = '{"session_id":"persisted","channel_id":"web","title":"Keep me"}'
    (session / "metadata.json").write_text(metadata, encoding="utf-8")
    original = {"content": "original persisted history"}
    (session / "history.jsonl").write_text(json.dumps(original) + "\n", encoding="utf-8")
    owner_path = data / "agent" / "lifecycle" / "resources" / "session_persisted.json"
    endpoint = (str(tmp_path / "worker.pipe") if os.name == "nt"
                else f"/tmp/jw-owner-{uuid.uuid4().hex[:8]}.sock")
    first = RuntimeSupervisor(Readiness(), forwarder=EventForwarder(), socket_path=endpoint, max_restarts=0)
    second_endpoint = endpoint + "-contender" if replacement == "live-owner" else endpoint
    second = RuntimeSupervisor(Readiness(), forwarder=EventForwarder(), socket_path=second_endpoint, max_restarts=0)
    process = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 20)
        process = first._process
        assert await first._readiness.wait_agent_ready(30)
        before = await execute(first, "before replacement")
        old_pid = first.worker_pid
        old_process = psutil.Process(old_pid)
        old_created = old_process.create_time()
        assert before["pid"] == old_pid
        assert before["history_before"] == [original]
        assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == old_pid
        if replacement == "live-owner":
            await asyncio.wait_for(second.run_until_ipc_ready(), 30)
            assert await second._readiness.wait_agent_ready(30)
            assert second.worker_pid != old_pid
            rejected = await execute(second, "must not be written", expect_error=True)
            assert "another live AgentServer owns the session runtime" in json.dumps(rejected)
            assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == old_pid
            continued = await execute(first, "original owner still works")
            assert continued["pid"] == old_pid
            assert [item["content"] for item in continued["history_before"]] == [
                original["content"], "before replacement",
            ]
            return
        # Detach the Front so its process watcher cannot reap the old child.
        # Retain the Popen owner until finally; Linux PID 1 is never asked to reap it.
        await first.stop()
        if replacement != "different-build":
            assert old_process.create_time() == old_created
            old_process.kill()
            if replacement == "exited-and-reaped":
                assert await process.wait(timeout=10) is not None
            else:
                async with asyncio.timeout(10):
                    # The leader can become Z before the remaining threads
                    # release their shared listener and lifetime lease.
                    while (old_process.status() != psutil.STATUS_ZOMBIE
                           or old_process.num_threads() != 1
                           or endpoint_is_listening(endpoint)):
                        await asyncio.sleep(.01)
                assert old_process.create_time() == old_created
            assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == old_pid
        monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "owner-recovery-new-build")
        await asyncio.wait_for(second.run_until_ipc_ready(), 30)
        assert await second._readiness.wait_agent_ready(30)
        assert second.worker_pid != old_pid
        after = await execute(second, "after replacement")
        assert after["pid"] == second.worker_pid
        assert after["session_id"] == "persisted"
        assert [item["content"] for item in after["history_before"]] == [
            original["content"], "before replacement",
        ]
        assert json.loads(owner_path.read_text(encoding="utf-8"))["runtime_owner"]["pid"] == second.worker_pid
        assert (session / "metadata.json").read_text(encoding="utf-8") == metadata
        history = [json.loads(line)["content"] for line in (session / "history.jsonl").read_text(encoding="utf-8").splitlines()]
        assert history == [original["content"], "before replacement", "after replacement"]
        if replacement == "unreaped-zombie":
            assert old_process.status() == psutil.STATUS_ZOMBIE
            assert old_process.num_threads() == 1
    finally:
        try:
            await second.stop(terminate_worker=True)
        finally:
            try:
                await first.stop(terminate_worker=True)
            finally:
                if process is not None:
                    await process.stop(timeout=5)
                    assert process.returncode is not None
