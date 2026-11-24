# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import os
import sys

import pytest
from websockets.legacy.server import serve

from jiuwenswarm.acp.session_mcp import MCP_CALL_METHOD, MCP_TOOLS_PARAM


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_close", [True, False])
async def test_stdio_tool_lifetime_and_gateway_round_trip(tmp_path, explicit_close):
    fixture = tmp_path / "server.py"
    fixture.write_text(
        'from mcp.server.fastmcp import FastMCP\n'
        'import os\n'
        'from pathlib import Path\n'
        'Path("server.pid").write_text(str(os.getpid()))\n'
        'app = FastMCP("fixture")\n'
        '@app.tool()\n'
        'def echo(message: str) -> str:\n'
        '    return os.environ["FIXTURE_PREFIX"] + message\n'
        'app.run(transport="stdio")\n',
    )
    requests = asyncio.Queue()

    async def gateway(ws):
        async for line in ws:
            frame = json.loads(line)
            if frame.get("method") == "chat.send":
                params = frame["params"]
                await requests.put(params)
                tool, = params[MCP_TOOLS_PARAM]
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": "call-1", "method": MCP_CALL_METHOD,
                    "params": {
                        "name": tool["name"], "token": tool["token"],
                        "arguments": {"message": "hello"}, "sessionId": params["session_id"],
                    },
                }))
                response = json.loads(await ws.recv())
                await requests.put(response)
                for event, extra in (
                    ("chat.final", {"content": "Done"}),
                    ("chat.processing_status", {"is_processing": False}),
                ):
                    await ws.send(json.dumps({
                        "type": "event", "event": event,
                        "payload": {"session_id": params["session_id"], **extra},
                    }))

    async with serve(gateway, "127.0.0.1", 0) as server:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "jiuwenswarm.acp.provider", "--gateway-url",
            f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/acp",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        async def request(rpc_id, method, params):
            process.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params,
            }).encode() + b"\n")
            await process.stdin.drain()
            async with asyncio.timeout(20):
                while line := await process.stdout.readline():
                    response = json.loads(line)
                    if response.get("id") == rpc_id:
                        return response
            pytest.fail("ACP process closed stdout")

        try:
            session = await request(1, "session/new", {
                "cwd": str(tmp_path),
                "mcpServers": [{
                    "name": "fixture", "command": sys.executable, "args": [str(fixture)],
                    "env": [{"name": "FIXTURE_PREFIX", "value": "MCP:"}],
                }],
            })
            session_id = session["result"]["sessionId"]
            pid = int((tmp_path / "server.pid").read_text())
            for rpc_id in (2, 3):
                prompt = await request(rpc_id, "session/prompt", {
                    "sessionId": session_id, "prompt": [{"type": "text", "text": "Use the MCP tool"}],
                })
                assert prompt["result"]["stopReason"] == "end_turn"
                params, response = requests.get_nowait(), requests.get_nowait()
                assert "mcpServers" not in params
                tool, = params[MCP_TOOLS_PARAM]
                assert tool["name"] == "mcp__fixture__echo"
                assert response["result"]["content"][0]["text"] == "MCP:hello"
                assert int((tmp_path / "server.pid").read_text()) == pid
            if explicit_close:
                response = await request(4, "session/close", {"sessionId": session_id})
                assert response["result"] == {}
            else:
                # Python 3.11's communicate() leaves stdin open when no input is given.
                process.stdin.close()
                await asyncio.wait_for(process.communicate(), 2)
                assert process.returncode == 0
            if os.name == "posix":
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.communicate(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,path", [("http", "/mcp"), ("sse", "/sse")])
async def test_remote_mcp_tool_call_with_headers(tmp_path, transport, path):
    import socket

    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Context

    from jiuwenswarm.acp.session_mcp import SessionMcp, _session_servers

    mcp = FastMCP("fixture", stateless_http=True, json_response=True)

    @mcp.tool()
    def echo(message: str, ctx: Context) -> str:
        return ctx.request_context.request.headers["x-fixture"] + message

    app = mcp.streamable_http_app() if transport == "http" else mcp.sse_app()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="auto"))
        worker = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            params = {
                "cwd": str(tmp_path),
                "mcpServers": [{
                    "type": transport, "name": "remote", "url": f"http://127.0.0.1:{port}{path}",
                    "headers": [{"name": "X-Fixture", "value": "remote:"}],
                }],
            }
            first = SessionMcp(_session_servers(params))
            try:
                tool, = await first.declarations()
                assert tool["name"] == "mcp__remote__echo"
                result = await first.call(tool["name"], {"message": "hello"})
                assert result["content"][0]["text"] == "remote:hello"
            finally:
                await first.close()
            assert not first._tools
        finally:
            server.should_exit = True
            await asyncio.wait_for(worker, 5)


@pytest.mark.parametrize("entry", [
    {"type": "http", "name": "remote", "url": "file:///tmp/x"},
    {"type": "sse", "name": "remote", "url": "http://localhost:bad"},
    {"type": "http", "name": "remote", "url": "http://localhost/mcp",
     "headers": [{"name": "X-Test", "value": "one\r\ntwo"}]},
])
def test_invalid_remote_transport_parameters(tmp_path, entry):
    from jiuwenswarm.acp.session_mcp import _session_servers

    with pytest.raises(ValueError):
        _session_servers({"cwd": str(tmp_path), "mcpServers": [entry]})
