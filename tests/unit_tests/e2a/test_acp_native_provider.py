# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import os
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
async def test_native_closes_the_gateway_connection_when_stdin_ends():
    closed = asyncio.get_running_loop().create_future()

    async def gateway(ws):
        await ws.wait_closed()
        closed.set_result(ws.close_code)

    async with serve(gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        process = await _provider("--gateway-url", f"ws://127.0.0.1:{port}/acp")
        try:
            # The provider connects before it reads stdin; end the session then.
            async with asyncio.timeout(8):
                while not server.websockets:
                    await asyncio.sleep(0.05)
            process.stdin.close()
            await asyncio.wait_for(process.communicate(), 8)
            assert process.returncode == 0
            # 1000 is a closing handshake; a socket left to drop at exit is 1006.
            assert await asyncio.wait_for(closed, 5) == 1000
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


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
                ("chat.delta", {"content": "Working.\n"}),
                ("chat.tool_call", {"tool_call_id": "phase", "name": "fixture"}),
                ("chat.delta", {"content": "Do"}),
                ("chat.delta", {"content": "ne"}),
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
            assert [
                item["content"]["text"] for item in updates
                if item.get("sessionUpdate") == "agent_message_chunk"
            ] == ["Working.\n", "Do", "ne"]
            assert {
                "sessionUpdate": "usage_update", "size": 1024, "used": 42,
                "_meta": {"usage": usage},
            } in updates
        finally:
            # Python 3.11's communicate() leaves stdin open when no input is given.
            process.stdin.close()
            try:
                await asyncio.wait_for(process.communicate(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise


@pytest.mark.asyncio
async def test_native_load_in_a_second_process_and_switch_model(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from jiuwenswarm.acp.session_mcp import MCP_CALL_METHOD, MCP_TOOLS_PARAM
    from jiuwenswarm.acp.session_protocol import ACP_SESSION_EXTENSION, ACP_THINKING_PARAM
    from jiuwenswarm.runtime.model_catalog import build_model_catalog
    from jiuwenswarm.server.handlers.acp_session import native_acp_session_response
    from jiuwenswarm.server.runtime.session import lifecycle, session_metadata

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    fixture = tmp_path / "mcp_server.py"
    fixture.write_text(
        'from mcp.server.fastmcp import FastMCP\n'
        'import os\n'
        'app = FastMCP("fixture")\n'
        '@app.tool()\n'
        'def process_id() -> str:\n'
        '    return str(os.getpid())\n'
        'app.run(transport="stdio")\n',
    )
    servers = [{"name": "fixture", "command": sys.executable, "args": [str(fixture)]}]

    entries = [
        {
            "model_client_config": {"model_name": "primary", "api_key": "test", "api_base": "https://api.openai.com"},
            "is_default": True,
        },
        {
            "model_client_config": {"model_name": "secondary", "api_key": "test", "api_base": "https://api.openai.com"},
            "alias": "Secondary",
        },
    ]
    monkeypatch.setattr("jiuwenswarm.common.config.get_default_models", lambda: entries)

    def list_models(*, current_selection):
        return build_model_catalog(entries, current_selection=current_selection)

    async def start():
        pass

    runtime = SimpleNamespace(start=start, list_model_capabilities=list_models)
    request = SimpleNamespace(channel_id="acp", user_id="test-user")
    turns = asyncio.Queue()

    async def gateway(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["method"] == "initialize":
                ctx = SimpleNamespace(**vars(request), params=msg["params"])
                payload = await native_acp_session_response(runtime, ctx)
                await ws.send(json.dumps({
                    "type": "res", "id": msg["id"], "ok": True, "payload": payload or {},
                }))
            elif msg["method"] == "chat.send":
                turn = msg["params"]
                tool, = turn[MCP_TOOLS_PARAM]
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": "mcp-call", "method": MCP_CALL_METHOD,
                    "params": {"sessionId": turn["session_id"], "name": tool["name"],
                               "token": tool["token"], "arguments": {}},
                }))
                reply = json.loads(await ws.recv())
                await turns.put((turn, int(reply["result"]["content"][0]["text"])))
                await ws.send(json.dumps({
                    "type": "event", "event": "chat.processing_status",
                    "payload": {
                        "is_processing": False, "request_id": msg["id"],
                        "session_id": msg["params"]["session_id"],
                    },
                }))

    async def run_client(url, operations):
        client = await _provider("--gateway-url", url)

        async def rpc(method, params):
            client.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": method, "method": method, "params": params,
            }).encode() + b"\n")
            await client.stdin.drain()
            while line := await asyncio.wait_for(client.stdout.readline(), 8):
                frame = json.loads(line)
                if frame.get("id") == method:
                    return frame
            pytest.fail(f"Missing response for {method}")

        try:
            initialized = await rpc("initialize", {"protocolVersion": 1})
            capabilities = initialized["result"]["agentCapabilities"]
            assert capabilities["loadSession"]
            assert capabilities["mcpCapabilities"] == {"http": True, "sse": True}
            return await operations(rpc)
        finally:
            client.stdin.close()
            async with asyncio.timeout(8):
                await client.communicate()

    async def first_client(rpc):
        session = (await rpc("session/new", {"cwd": str(cwd), "mcpServers": servers}))["result"]
        sid = session["sessionId"]
        assert session["models"] == {
            "currentModelId": "primary#0",
            "availableModels": [
                {"modelId": "primary#0", "name": "primary"},
                {"modelId": "secondary#1", "name": "Secondary"},
            ],
        }
        invalid = await rpc("session/set_model", {"sessionId": sid, "modelId": "bogus"})
        assert invalid["error"]["code"] == -32602
        switched = await rpc("session/set_model", {"sessionId": sid, "modelId": "secondary#1"})
        assert switched["result"]["models"]["currentModelId"] == "secondary#1"
        option, = switched["result"]["configOptions"]
        assert option["category"] == "thought_level"
        config = {"sessionId": sid, "configId": "thinking"}
        invalid = await rpc("session/set_config_option", {**config, "value": "bogus"})
        assert invalid["error"]["code"] == -32602
        updated = await rpc("session/set_config_option", {**config, "value": "high"})
        assert updated["result"]["configOptions"][0]["currentValue"] == "high"
        prompt = await rpc("session/prompt", {
            "sessionId": sid, ACP_THINKING_PARAM: "off", "prompt": [{"type": "text", "text": "First turn"}],
        })
        assert prompt["result"]["stopReason"] == "end_turn"
        turn, pid = turns.get_nowait()
        assert turn[ACP_THINKING_PARAM] == "high"
        assert "configOptions" not in turn
        updated = await rpc("session/set_config_option", {**config, "value": "default"})
        assert updated["result"]["configOptions"][0]["currentValue"] == "default"
        await rpc("session/prompt", {
            "sessionId": sid, "prompt": [{"type": "text", "text": "With defaults"}],
        })
        assert turns.get_nowait()[0][ACP_THINKING_PARAM] is None
        return sid, pid

    async def second_client(rpc):
        load = {"sessionId": session_id, "cwd": str(cwd)}
        missing = await rpc("session/load", {**load, "sessionId": "acp_" + "0" * 32})
        assert missing["error"]["code"] == -32002
        result = (await rpc("session/load", {**load, "mcpServers": servers}))["result"]
        assert result["sessionId"] == session_id
        assert result["models"]["currentModelId"] == "secondary#1"
        prompt = await rpc("session/prompt", {
            "sessionId": session_id,
            "model_name": "primary#0",
            "eternal_conversation_enabled": False,
            "prompt": [{"type": "text", "text": "Continue"}],
        })
        assert prompt["result"]["stopReason"] == "end_turn"
        turn, pid = turns.get_nowait()
        assert pid != first_pid
        assert turn["session_id"] == session_id
        assert turn["model_name"] == "secondary#1"
        assert turn["eternal_conversation_enabled"] is True
        return pid

    async with serve(gateway, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/acp"
        session_id, first_pid = await run_client(url, first_client)
        session_metadata.remove_session_metadata_cache(session_id)
        stored = session_metadata.get_session_metadata(session_id, cache_bust=True)
        assert stored["persist_session"] is True
        assert stored["model"] == "secondary#1"
        second_pid = await run_client(url, second_client)

    if os.name == "posix":
        for pid in (first_pid, second_pid):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    def archived(_sid):
        raise lifecycle.LifecycleError("SESSION_ARCHIVED", "session has been archived")

    monkeypatch.setattr(lifecycle, "guard", archived)
    response = await native_acp_session_response(runtime, SimpleNamespace(
        **vars(request), params={ACP_SESSION_EXTENSION: {
            "operation": "resume", "sessionId": session_id, "cwd": str(cwd),
        }},
    ))
    assert response[ACP_SESSION_EXTENSION]["error"] == {
        "code": -32002, "message": "Session not found or expired",
    }
