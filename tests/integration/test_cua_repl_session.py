# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the desktop-only SDK worker without starting a native driver."""

import asyncio

import pytest

from jiuwenswarm.agents.harness.common.tools.cua_runtime import ReplRuntime
from jiuwenswarm.agents.harness.common.tools.cua_repl import CuaReplResetTool, CuaReplTool


def test_real_worker_reset_clears_variables_and_never_replays_code(monkeypatch, tmp_path):
    sdk = pytest.importorskip("computer_use_mcp.session")
    monkeypatch.setenv("COMPUTER_USE_LOG_DIR", str(tmp_path / "logs"))

    async def scenario():
        session = sdk.ExecutionSession()
        runtime = ReplRuntime(session=session)
        repl = CuaReplTool(runtime, "integration")
        reset = CuaReplResetTool(repl, "integration")
        worker = None
        try:
            initial = await repl.invoke({"code": "value = 7; print(value)"})
            assert initial.success and initial.data["content"] == "7"
            assert not (await repl.invoke({"code": "raise ValueError('ordinary code error')"})).success
            retained = await repl.invoke({"code": "print(value)"})
            assert retained.success and retained.data["content"] == "7"
            previous_worker = session._child
            assert (await reset.invoke({})).success
            assert previous_worker.returncode is not None
            missing = await repl.invoke({"code": "print(value)"})
            assert not missing.success and "NameError" in missing.error
            fresh = await repl.invoke({"code": "value = 9; print(value)"})
            assert fresh.success and fresh.data["content"] == "9"
            worker = session._child
        finally:
            await runtime.close()
        assert worker.returncode is not None and session._child is None

    asyncio.run(scenario())


def test_real_worker_close_interrupts_pending_execution_without_waiting_for_watchdog(monkeypatch, tmp_path):
    sdk = pytest.importorskip("computer_use_mcp.session")
    monkeypatch.setenv("COMPUTER_USE_LOG_DIR", str(tmp_path / "logs"))

    async def scenario():
        session = sdk.ExecutionSession(timeout_ms=45000)
        runtime = ReplRuntime(session=session)
        pending = None
        try:
            assert not (await runtime.execute("print('ready')")).isError
            pending = asyncio.create_task(runtime.execute("await __import__('asyncio').Event().wait()"))
            for _ in range(100):
                if session._pending is not None:
                    break
                await asyncio.sleep(0.01)
            assert session._pending is not None
            await asyncio.wait_for(runtime.close(), 3)
            assert (await asyncio.wait_for(pending, 3)).isError
            assert runtime.closing
            with pytest.raises(RuntimeError, match="closed"):
                await runtime.execute("print('must not run')")
        finally:
            # Exercise the public SDK cleanup even if wrapper close regresses.
            await session.close()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
