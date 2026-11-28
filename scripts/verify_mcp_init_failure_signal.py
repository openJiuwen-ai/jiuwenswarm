"""Verify the patched jiuwenswarm MCP worker init-failure behaviour in a live tree.

Usage (run from the jiuwenswarm repo root, or point --root at it):

    .venv/Scripts/python.exe scripts/verify_mcp_init_failure_signal.py --root .

It drives `_run_mcp_worker` with a fake MCP session factory, exactly like
`tests/unit_tests/common/test_mcp_init_failure_signal.py`, and prints what the
caller (and therefore the model) now receives.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock


async def run(root: Path) -> int:
    sys.path.insert(0, str(root))
    from jiuwenswarm.common import mcp_config

    failures = 0

    async def drive(exc: BaseException, close_reason: str = "", *, reset: bool = True):
        if reset:
            mcp_config._request_scoped_mcp_sessions.clear()
            mcp_config._request_scoped_mcp_init_failures.clear()
            mcp_config._request_scoped_mcp_dead.clear()
        original = mcp_config._enter_remote_mcp_session
        mcp_config._enter_remote_mcp_session = AsyncMock(side_effect=exc)
        try:
            worker = mcp_config._PooledMcpWorker("xlsx-mcp")
            worker.close_reason = close_reason
            mcp_config._request_scoped_mcp_sessions[("verify-req", "xlsx-mcp")] = worker
            request = mcp_config._McpCallRequest("read_cells", {})
            worker.queue.put_nowait(request)
            worker.task = asyncio.create_task(
                mcp_config._run_mcp_worker({"_mcp_client_type": "sse"}, worker)
            )
            result = (await asyncio.gather(request.future, return_exceptions=True))[0]
            try:
                await asyncio.wait_for(worker.task, 1)
            except asyncio.CancelledError:
                pass
            return result
        finally:
            mcp_config._enter_remote_mcp_session = original

    print("== 1) credential expired (HTTP 403) ==")
    result = await drive(
        RuntimeError("Error POSTing to endpoint (HTTP 403): XLSX_CAPABILITY_INVALID"),
    )
    print(f"   caller got: {type(result).__name__}: {result}")
    if "401/403" not in str(result) or "Do not retry" not in str(result):
        print("   FAIL: no actionable guidance")
        failures += 1
    if ("verify-req", "xlsx-mcp") not in mcp_config._request_scoped_mcp_dead:
        print("   FAIL: connector not marked unrecoverable")
        failures += 1

    print("== 2) second acquire must fail fast (no new worker) ==")
    try:
        await mcp_config.acquire_request_scoped_mcp_session(
            "verify-req",
            "xlsx-mcp",
            {"_mcp_client_type": "sse"},
        )
        print("   FAIL: acquire did not raise")
        failures += 1
    except mcp_config.McpWorkerStoppedError as exc:
        print(f"   caller got: McpWorkerStoppedError: {str(exc)[:90]}...")

    print("== 3) init cancelled (CancelledError) keeps cause ==")
    result = await drive(asyncio.CancelledError(), close_reason="shutdown")
    print(f"   caller got: {type(result).__name__}: {result}")
    if "CancelledError" not in str(result) or "close_reason=shutdown" not in str(
        result
    ):
        print("   FAIL: cause/close_reason missing")
        failures += 1

    print("== 4) three generic failures stop the rebuild ==")
    messages = []
    for index in range(mcp_config._MCP_INIT_FAILURE_LIMIT):
        messages.append(
            str(await drive(RuntimeError("host unreachable"), reset=index == 0))
        )
    print(f"   #1 {messages[0]}")
    print(f"   #3 {messages[2]}")
    if "stop retrying" not in messages[2]:
        print("   FAIL: no stop signal at the limit")
        failures += 1

    mcp_config._request_scoped_mcp_sessions.clear()
    mcp_config._request_scoped_mcp_init_failures.clear()
    mcp_config._request_scoped_mcp_dead.clear()
    print(
        f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}"
    )
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    return asyncio.run(run(Path(args.root).resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
