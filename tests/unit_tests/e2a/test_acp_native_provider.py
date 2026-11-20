# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import sys

import pytest
from websockets.legacy.server import serve

from jiuwenswarm.common.version import get_runtime_version


async def _provider(*args):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "jiuwenswarm.acp.provider", *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.asyncio
async def test_native_version_and_unreachable_gateway():
    async with serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/acp"

    version = await _provider("--version", "--gateway-url", url)
    stdout, stderr = await asyncio.wait_for(version.communicate(), 5)
    assert (version.returncode, stdout.decode().strip(), stderr) == (
        0, get_runtime_version(), b"",
    )

    disconnected = await _provider("--gateway-url", url)
    stdout, stderr = await asyncio.wait_for(disconnected.communicate(), 8)
    assert disconnected.returncode != 0
    assert not stdout
    assert f"JiuwenSwarm Gateway unreachable: {url}" in stderr.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("aliased", [False, True])
async def test_native_uses_session_cwd_and_forwards_gateway_usage(tmp_path, aliased):
    requests = asyncio.Queue()

    async def gateway(ws):
        async for frame in ws:
            req = json.loads(frame)
            if req["method"] != "chat.send":
                continue
            await requests.put(req)
            usage = {"input_tokens": 42, "output_tokens": 3, "total_tokens": 45, "cache_tokens": 7}
            for event, fields in (
                ("chat.delta", {"content": "Done"}),
                ("context.usage", {"context_max": 1024, "tokens_used": 42}),
                ("chat.usage_summary", {"usage": usage}),
                ("chat.final", {"content": "Done"}),
                ("chat.processing_status", {"is_processing": False}),
            ):
                usage_event = event in ("context.usage", "chat.usage_summary")
                payload = {
                    "session_id": "gateway-session" if aliased and usage_event else req["params"]["session_id"],
                    "request_id": "gateway-request" if aliased and usage_event else req["id"],
                    "execution_id": "gateway-execution",
                    **fields,
                }
                await ws.send(json.dumps({"type": "event", "event": event, "payload": payload}))

    async with serve(gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        process = await _provider("--gateway-url", f"ws://127.0.0.1:{port}/acp")

        async def request(rpc_id, method, params):
            process.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params,
            }).encode() + b"\n")
            await process.stdin.drain()
            updates = []
            while line := await asyncio.wait_for(process.stdout.readline(), 8):
                frame = json.loads(line)
                if frame.get("id") == rpc_id:
                    return frame, updates
                if frame.get("method") == "session/update":
                    updates.append(frame["params"]["update"])
            pytest.fail("ACP process closed stdout before responding")

        try:
            invalid, _ = await request(1, "session/new", {"cwd": "."})
            assert invalid["error"]["code"] == -32602
            new, _ = await request(2, "session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = new["result"]["sessionId"]
            prompt, updates = await request(3, "session/prompt", {
                "sessionId": session_id, "cwd": "/", "project_dir": "/", "workspace_dir": "/",
                "prompt": [{"type": "text", "text": "Hello"}],
            })
            wire = requests.get_nowait()
            for field in ("cwd", "project_dir", "workspace_dir"):
                assert wire["params"][field] == str(tmp_path)
            usage = {"inputTokens": 42, "outputTokens": 3, "totalTokens": 45, "cachedReadTokens": 7}
            assert prompt["result"] == {"stopReason": "end_turn", "usage": usage}
            assert {
                "sessionUpdate": "usage_update", "size": 1024, "used": 42,
                "_meta": {"usage": usage},
            } in updates
        finally:
            try:
                await asyncio.wait_for(process.communicate(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
