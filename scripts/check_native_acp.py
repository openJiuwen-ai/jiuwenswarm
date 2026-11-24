#!/usr/bin/env python3
"""Verify the native ACP provider against a running, configured Gateway."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile

logger = logging.getLogger("check_native_acp")


def _require(condition: bool, detail: object) -> None:
    if not condition:
        raise RuntimeError(f"Native ACP check failed: {detail}")


async def check(gateway_url: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory).resolve()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "jiuwenswarm.acp.provider", "--gateway-url", gateway_url,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

        async def send(method, params, *, rpc_id=None):
            request = {"jsonrpc": "2.0", "method": method, "params": params}
            if rpc_id is not None:
                request["id"] = rpc_id
            process.stdin.write(json.dumps(request).encode() + b"\n")
            await process.stdin.drain()

        async def receive(rpc_id):
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                frame = json.loads(line)
                if frame.get("id") == rpc_id:
                    if "error" in frame:
                        raise RuntimeError(frame["error"])
                    return frame["result"]
                update = frame.get("params", {}).get("update", {})
                if update.get("sessionUpdate") == "usage_update":
                    logger.info(json.dumps(frame))
            raise RuntimeError("ACP closed stdout")

        async def new_session(rpc_id):
            await send("session/new", {"cwd": str(cwd), "mcpServers": []}, rpc_id=rpc_id)
            return (await receive(rpc_id))["sessionId"]

        async def prompt(session_id, rpc_id, text):
            await send("session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": text}],
            }, rpc_id=rpc_id)

        try:
            await send("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "terminal": False, "fs": {"readTextFile": False, "writeTextFile": False},
                },
            }, rpc_id=1)
            await receive(1)
            session = await new_session(2)
            await prompt(session, 3, "Use bash to run exactly: pwd > acp-cwd-check.txt")
            result = await receive(3)
            logger.info(json.dumps({"promptResult": result}))
            _require(result["stopReason"] == "end_turn", result)
            reported_cwd = Path((cwd / "acp-cwd-check.txt").read_text().strip()).resolve()
            _require(reported_cwd == cwd, f"prompt ran in {reported_cwd}, expected {cwd}")
            _require(result["usage"]["inputTokens"] > 0, result)
            _require(result["usage"]["outputTokens"] > 0, result)

            if os.name != "posix":
                return
            cancel_session = await new_session(4)
            await prompt(cancel_session, 5, (
                "Use bash in one call to run exactly this test command. "
                "Do not background the command: sleep 60 & echo $! > acp-sleep.pid; wait"
            ))
            pid_file = cwd / "acp-sleep.pid"
            while not pid_file.exists() or not pid_file.read_text().strip():
                await asyncio.sleep(0.1)
            pid = int(pid_file.read_text())
            os.kill(pid, 0)
            await send("session/cancel", {"sessionId": cancel_session})
            cancelled = await receive(5)
            _require(cancelled["stopReason"] == "cancelled", cancelled)

            async with asyncio.timeout(8):
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        logger.info("Cancelled prompt and reaped its child process")
                        break
                    await asyncio.sleep(0.1)
        finally:
            if process.returncode is None:
                process.terminate()
            await process.communicate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gateway_url")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    asyncio.run(asyncio.wait_for(check(args.gateway_url), timeout=60))


if __name__ == "__main__":
    main()
