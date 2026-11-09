# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit REPL recovery must retire state without replaying desktop input."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.common.tools import cua_repl
from jiuwenswarm.agents.harness.common.tools.cua_runtime import ReplRuntime, desktop_lane


def result(text, *, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


class Session:
    """Model the SDK's persistent worker and its public reset/close contract."""

    def __init__(self):
        self.codes = []
        self.variables = {}
        self.retired = False
        self.resets = self.closes = 0
        self.reset_result = result("Variables and handles cleared; observe before continuing.")
        self.close_result = result("Closed")
        self.reset_started = asyncio.Event()
        self.reset_release = asyncio.Event()
        self.reset_release.set()

    async def execute(self, code, title=None):
        self.codes.append(code)
        if self.retired:
            return result("Native session is retired", error=True)
        if code == "retire":
            self.retired = True
            return result("Native cleanup was unconfirmed", error=True)
        if code == "syntax error":
            return result("SyntaxError", error=True)
        if code == "save variable":
            self.variables["value"] = 7
        return result(str(self.variables.get("value", "fresh observation")))

    async def reset(self):
        self.resets += 1
        self.reset_started.set()
        await self.reset_release.wait()
        if isinstance(self.reset_result, Exception):
            raise self.reset_result
        if not self.reset_result["isError"]:
            self.retired = False
            self.variables.clear()
        return self.reset_result

    async def close(self):
        self.closes += 1
        await self.reset_release.wait()
        return self.close_result


def test_retired_session_can_be_explicitly_reset_without_replaying_input():
    async def scenario():
        session = Session()
        runtime = ReplRuntime(session=session)
        assert (await runtime.execute("retire")).isError
        assert (await runtime.execute("observe")).isError
        assert session.resets == 0
        assert not (await runtime.reset()).isError
        assert not (await runtime.execute("observe")).isError
        assert session.codes == ["retire", "observe", "observe"]
        assert session.resets == 1

    asyncio.run(scenario())


def test_python_error_preserves_variables_until_explicit_reset():
    async def scenario():
        session = Session()
        runtime = ReplRuntime(session=session)
        await runtime.execute("save variable")
        assert (await runtime.execute("syntax error")).isError
        assert (await runtime.execute("observe")).content[0].text == "7"
        assert session.resets == session.closes == 0
        await runtime.reset()
        assert (await runtime.execute("observe")).content[0].text == "fresh observation"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [result("Still cleaning up", error=True), RuntimeError("Cleanup failed")])
def test_failed_reset_blocks_input_until_own_explicit_retry(failure):
    async def scenario():
        session = Session()
        repl = cua_repl.CuaReplTool(ReplRuntime(session=session), "owner")
        reset = cua_repl.CuaReplResetTool(repl, "owner")
        session.reset_result = failure
        assert not (await reset.invoke({})).success
        assert not (await repl.invoke({"code": "must not run"})).success
        other = cua_repl.CuaReplTool(ReplRuntime(session=Session()), "other")
        assert not (await other.invoke({"code": "must not run"})).success
        assert not (await cua_repl.CuaReplResetTool(other, "other").invoke({})).success
        assert session.codes == []
        session.reset_result = result("Cleaned up")
        assert (await reset.invoke({})).success
        assert (await repl.invoke({"code": "observe"})).success
        assert session.codes == ["observe"]

    asyncio.run(scenario())


def test_reset_cannot_clear_another_owners_cleanup_blocker():
    async def scenario():
        first, second = ReplRuntime(session=Session()), ReplRuntime(session=Session())
        first.session.reset_result = result("First pending", error=True)
        second.session.reset_result = result("Second pending", error=True)
        assert (await first.reset()).isError
        assert (await second.reset()).isError
        first_repl = cua_repl.CuaReplTool(first, "first")
        second_repl = cua_repl.CuaReplTool(second, "second")
        first.session.reset_result = result("First cleaned up")
        assert (await cua_repl.CuaReplResetTool(first_repl, "first").invoke({})).success
        assert "Second pending" in desktop_lane().blocker
        with pytest.raises(RuntimeError, match="Second pending"):
            async with desktop_lane().acquire():
                pytest.fail("Another owner's pending cleanup must still block input")
        second.session.reset_result = result("Second cleaned up")
        assert (await cua_repl.CuaReplResetTool(second_repl, "second").invoke({})).success
        assert desktop_lane().blocker is None

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [None, {"code": "must not run"}, {"reset": True}])
def test_reset_tool_accepts_no_code_or_other_arguments(arguments):
    async def scenario():
        session = Session()
        repl = cua_repl.CuaReplTool(ReplRuntime(session=session), "owner")
        reset = cua_repl.CuaReplResetTool(repl, "owner")
        assert not (await reset.invoke(arguments)).success
        assert session.resets == 0
        assert session.codes == []

    asyncio.run(scenario())


def test_unused_runtime_reset_does_not_start_a_worker():
    async def scenario():
        runtime = ReplRuntime()
        assert not (await runtime.reset()).isError
        assert runtime.session is None

    asyncio.run(scenario())


def test_reset_waits_for_cleanup_on_cancellation_and_does_not_reopen_closed_runtime():
    async def scenario():
        session = Session()
        session.reset_release.clear()
        repl = cua_repl.CuaReplTool(ReplRuntime(session=session), "owner")
        resetting = asyncio.create_task(cua_repl.CuaReplResetTool(repl, "owner").invoke({}))
        await session.reset_started.wait()
        resetting.cancel()
        await asyncio.sleep(0)
        assert not resetting.done()
        assert desktop_lane().blocker
        session.reset_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(resetting, 1)
        assert repl.runtime.closing
        assert session.closes == 1
        assert session.codes == []
        assert not (await repl.invoke({"code": "must not run"})).success

    asyncio.run(scenario())


def test_close_waits_for_reset_and_queued_execution_cannot_resurrect_runtime():
    async def scenario():
        session = Session()
        session.reset_release.clear()
        runtime = ReplRuntime(session=session)
        resetting = asyncio.create_task(runtime.reset())
        await session.reset_started.wait()
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert runtime.closing
        await asyncio.sleep(0)
        assert session.closes == 1
        queued = asyncio.create_task(runtime.execute("must not run"))
        session.reset_release.set()
        await asyncio.wait_for(asyncio.gather(resetting, closing), 1)
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(queued, 1)
        assert session.codes == []
        assert session.closes == 1
        with pytest.raises(RuntimeError, match="closed"):
            await runtime.reset()

    asyncio.run(scenario())


@pytest.mark.parametrize("close_fails", [False, True])
def test_cancelled_reset_preserves_cancellation_when_cleanup_raises(close_fails):
    async def scenario():
        session = Session()
        session.reset_result = OSError("Reset cleanup failed")
        if close_fails:
            session.close_result = result("Close cleanup remains pending", error=True)
        session.reset_release.clear()
        repl = cua_repl.CuaReplTool(ReplRuntime(session=session), "owner")
        pending = asyncio.create_task(cua_repl.CuaReplResetTool(repl, "owner").invoke({}))
        await session.reset_started.wait()
        pending.cancel()
        await asyncio.sleep(0)
        session.reset_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        assert pending.cancelled()
        assert repl.runtime.closing and session.closes == 1
        assert session.codes == []
        if close_fails:
            assert "Close cleanup remains pending" in desktop_lane().blocker

    asyncio.run(scenario())


def test_extension_registers_reset_and_rebinds_it_to_resumed_runtime(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails import cua_extension_rail

    monkeypatch.setattr(cua_extension_rail.sys, "platform", "win32")
    agent = MagicMock()
    agent.find_rails_by_type.return_value = (MagicMock(),)
    rail = cua_extension_rail.CuaExtensionRail()
    rail.init(agent)
    tools = {call.args[0].name: call.args[1] for call in agent.ability_manager.add_ability.call_args_list}
    assert set(tools) == {"cua_repl", "cua_repl_reset"}

    async def scenario():
        old = Session()
        rail.runtime.session = old
        rail.repl.last_failed_code, rail.repl.same_failures = "old", 3
        await rail.close()
        await rail.before_invoke(SimpleNamespace(session=None))
        current = Session()
        rail.runtime.session = current
        assert (await tools["cua_repl_reset"].invoke({})).success
        assert current.resets == 1 and old.resets == 0
        assert rail.repl.last_failed_code is None and rail.repl.same_failures == 0
        await rail.close()

    asyncio.run(scenario())
