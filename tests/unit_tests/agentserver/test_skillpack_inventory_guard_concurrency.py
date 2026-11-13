# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real inventory-lock contracts for concurrent confirmed Pack tool guards."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from filelock import FileLock, Timeout
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolOutput
from openjiuwen.core.foundation.tool.exposure import ToolExposure
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent, InvokeInputs
from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.tools.tool_discovery.tool_call import ToolCallTool

from jiuwenswarm.agents.harness.common.rails.symphony.skillpack_route import SkillPackRouter
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import HubPackageDownloader
from jiuwenswarm.server.runtime.skill import skillpack_inventory
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.skillpack_install import SkillPackInstaller
from tests.unit_tests.agentserver.test_skillpack_backend import HubFixture


def _file_lock_available(skills: Path) -> bool:
    """Probe the real file lock from a thread that does not own the scan."""
    try:
        with FileLock(str(skills / ".skillpack/inventory.lock"), timeout=0):
            return True
    except Timeout:
        return False


@pytest_asyncio.fixture
async def installed_route(tmp_path):
    """Install real ZIPs and provenance; only the remote Hub is synthetic."""
    workspace = tmp_path / "workspace"
    manager = SkillManager(workspace_dir=str(workspace))
    assert manager._skills_dir.is_relative_to(tmp_path)
    assert manager._state_file.is_relative_to(tmp_path)
    hub = HubFixture()
    installer = SkillPackInstaller(
        manager._skills_dir, "https://localhost", hub,
        state_files=(manager._state_file, manager._agent_root / "agent-data.json"),
        downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
        enabled_for=manager.get_skill_enabled,
    )
    artifact = hub.artifacts["release-pack"]
    plan = await installer.prepare(artifact, hub.bodies[artifact.download_url])
    committed = await installer.commit(plan["plan_id"], on_committed=manager._register_skillpack_install)
    assert committed["success"]
    router = SkillPackRouter(manager=lambda: manager, toolkit=lambda: None, model=lambda: None)
    route = await router.choose("Run both members", ["release-pack"], mode="confirm")
    assert route is not None and route.token is not None and not route.blocked
    return SimpleNamespace(
        manager=manager, router=router, route=route, skills=manager._skills_dir,
        member=manager._skills_dir / "summary/SKILL.md",
    )


class _ScanGate:
    """Delay real scanning inside the real mutex, without replacing its result."""

    def __init__(self, context, monkeypatch, *, read_before_pause=False, fail=False, pause_on_call=1):
        self.started = threading.Event()
        self.release = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.calls = 0
        self.active_workers = 0
        self.worker_lock = threading.Lock()
        original_scan = skillpack_inventory.skillpack_inventory_snapshot
        original_snapshot = context.manager.skillpack_inventory_snapshot

        def scan(skills_dir, enabled_for):
            with self.worker_lock:
                self.calls += 1
                first = self.calls == pause_on_call
            if not first:
                return original_scan(skills_dir, enabled_for)
            snapshot = original_scan(skills_dir, enabled_for) if read_before_pause else None
            self.started.set()
            if not self.release.wait(20):
                raise TimeoutError("Test did not release the slow inventory read")
            if fail:
                raise OSError("Controlled inventory read failure")
            return snapshot if read_before_pause else original_scan(skills_dir, enabled_for)

        def tracked_snapshot(*, lock_timeout):
            assert lock_timeout == 5
            with self.worker_lock:
                self.active_workers += 1
                self.idle.clear()
            try:
                return original_snapshot(lock_timeout=lock_timeout)
            finally:
                with self.worker_lock:
                    self.active_workers -= 1
                    if not self.active_workers:
                        self.idle.set()

        monkeypatch.setattr(skillpack_inventory, "skillpack_inventory_snapshot", scan)
        monkeypatch.setattr(context.manager, "skillpack_inventory_snapshot", tracked_snapshot)

    async def wait_started(self):
        assert await asyncio.to_thread(self.started.wait, 5)

    async def finish(self, *tasks):
        self.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Cancelled to_thread callers can finish before their real workers do.
        assert await asyncio.to_thread(self.idle.wait, 10)


@pytest.mark.asyncio
async def test_same_route_parallel_guards_do_not_exhaust_inventory_lock_timeout(installed_route, monkeypatch):
    context = installed_route
    gate = _ScanGate(context, monkeypatch)
    first = asyncio.create_task(context.router.is_current(context.route))
    second = None
    try:
        await gate.wait_started()
        assert not await asyncio.to_thread(_file_lock_available, context.skills)
        second = asyncio.create_task(context.router.is_current(context.route))
        # Keep the first real file lock longer than the production five-second
        # acquisition budget. The next tool must wait outside that budget.
        done, _ = await asyncio.wait({second}, timeout=5.25)
        assert not done, "A same-request tool exhausted the inventory file-lock timeout"
        assert gate.calls == 1
        gate.release.set()
        assert await first
        assert await second
        assert gate.calls == 2  # Both tools obtained a fresh real snapshot.
        assert not context.route.blocked
    finally:
        await gate.finish(*[task for task in (first, second) if task is not None])
    assert await asyncio.to_thread(_file_lock_available, context.skills)


@pytest.mark.asyncio
async def test_queued_guard_reads_new_content_instead_of_reusing_previous_success(installed_route, monkeypatch):
    context = installed_route
    gate = _ScanGate(context, monkeypatch, read_before_pause=True)
    first = asyncio.create_task(context.router.is_current(context.route))
    second = None
    try:
        await gate.wait_started()
        second = asyncio.create_task(context.router.is_current(context.route))
        await asyncio.sleep(0)
        before = context.member.stat()
        content = context.member.read_text(encoding="utf-8")
        assert "ordinary" in content
        context.member.write_text(content.replace("ordinary", "modified"), encoding="utf-8")
        os.utime(context.member, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert context.member.stat().st_size == before.st_size
        gate.release.set()
        assert await first
        assert not await second
        assert gate.calls == 2
        assert context.route.blocked
    finally:
        await gate.finish(*[task for task in (first, second) if task is not None])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["content_changed", "read_error"])
async def test_first_invalid_guard_stops_queued_tools_before_another_scan(installed_route, monkeypatch, failure):
    context = installed_route
    gate = _ScanGate(context, monkeypatch, fail=failure == "read_error")
    first = asyncio.create_task(context.router.is_current(context.route))
    second = None
    try:
        await gate.wait_started()
        second = asyncio.create_task(context.router.is_current(context.route))
        await asyncio.sleep(0)
        if failure == "content_changed":
            context.member.write_text(context.member.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
        gate.release.set()
        if failure == "read_error":
            with pytest.raises(OSError, match="Controlled inventory read failure"):
                await first
        else:
            assert not await first
        assert not await second
        assert gate.calls == 1
        assert context.route.blocked and not context.route.member_started
    finally:
        await gate.finish(*[task for task in (first, second) if task is not None])
    assert await asyncio.to_thread(_file_lock_available, context.skills)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_target", ["holding_scan", "queued_guard"])
async def test_guard_cancellation_distinguishes_scanning_from_waiting_for_request_gate(
    installed_route, monkeypatch, cancel_target,
):
    context = installed_route
    gate = _ScanGate(context, monkeypatch)
    first = asyncio.create_task(context.router.is_current(context.route))
    second = None
    try:
        await gate.wait_started()
        second = asyncio.create_task(context.router.is_current(context.route))
        await asyncio.sleep(0)
        cancelled = first if cancel_target == "holding_scan" else second
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, timeout=1)
        assert context.route.blocked == (cancel_target == "holding_scan")
        assert not gate.release.is_set()
        assert not await asyncio.to_thread(_file_lock_available, context.skills)
        if cancel_target == "holding_scan":
            assert not await asyncio.wait_for(second, timeout=1)
        gate.release.set()
        if cancel_target == "queued_guard":
            assert await first
        assert gate.calls == 1
    finally:
        await gate.finish(*[task for task in (first, second) if task is not None])
    assert await asyncio.to_thread(_file_lock_available, context.skills)
    # Cancelling an unstarted validation must not poison the still-live route.
    assert await context.router.is_current(context.route) == (cancel_target == "queued_guard")
    replacement = await context.router.choose("Run both members", ["release-pack"], mode="confirm")
    assert replacement is not None and replacement.token is not None
    assert await context.router.is_current(replacement)


class _LifecycleTool(Tool):
    """Small tool bodies; Core owns their scheduling, deadlines and interrupts."""

    def __init__(self, name, operation):
        super().__init__(ToolCard(id=name, name=name, description="Lifecycle test", stateless=True))
        self.operation = operation

    async def invoke(self, inputs, **kwargs):
        return await self.operation()

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["wrapper_timeout", "hitl", "batch_abort"])
async def test_core_tool_lifecycle_preserves_only_live_pack_routes(installed_route, monkeypatch, interruption):
    """Exercise real Core batch/HITL and the wrapper's nested deadline scope."""
    context = installed_route
    # The first tool passes its guard. Its sibling then owns the actual file
    # lock while a third guard (and the wrapper's nested target) queues.
    gate = _ScanGate(context, monkeypatch, pause_on_call=2)
    ident = f"pack-lifecycle-{uuid4().hex}"
    callbacks = AgentCallbackManager(ident)
    abilities = AbilityManager()
    session = Session(session_id=ident)
    agent = SimpleNamespace(agent_callback_manager=callbacks, ability_manager=abilities)
    callback_context = AgentCallbackContext(agent=agent, inputs=InvokeInputs(query="Use the pack"), session=session)
    nested_cancelled = asyncio.Event()
    nested_entered = asyncio.Event()
    question_raised = asyncio.Event()
    body_calls = []
    registered = []

    async def guard(ctx):
        if ctx.inputs.tool_call.id.endswith(":target"):
            nested_entered.set()
        try:
            if not await context.router.is_current(context.route):
                ctx.request_force_finish({"reason": "skillpack_state_changed"})
        except asyncio.CancelledError:
            if ctx.inputs.tool_call.id.endswith(":target"):
                nested_cancelled.set()
            raise

    async def success():
        body_calls.append("success")
        return ToolOutput(success=True, data={"content": "done"})

    async def question():
        await gate.wait_started()
        question_raised.set()
        raise ToolInterruptException(InterruptRequest(message="Confirm a task input", interrupt_kind="ask_user"))

    def register(tool):
        Runner.resource_mgr.add_tool(tool)
        abilities.add(tool.card)
        registered.append(tool.card)
        return tool.card.name

    first_name = register(_LifecycleTool(f"{ident}-first", question if interruption == "hitl" else success))
    sibling_name = register(_LifecycleTool(f"{ident}-sibling", success))
    queued_name = register(_LifecycleTool(f"{ident}-queued", success))
    calls = [
        ToolCall(id="first", type="function", name=first_name, arguments="{}"),
        ToolCall(id="sibling", type="function", name=sibling_name, arguments="{}"),
        ToolCall(id="queued", type="function", name=queued_name, arguments="{}"),
    ]
    if interruption == "wrapper_timeout":
        progressive = ProgressiveToolRail(DeepAgentConfig())
        target = _LifecycleTool(f"{ident}-target", success)
        target.card.exposure = ToolExposure.DEFERRED
        target.card.set_exposure_declared(True)
        target_name = register(target)
        progressive._tool_search_registry = abilities
        matches = await progressive._search_tools(target_name, session=session)
        assert any(match.get("name") == target_name for match in matches)
        wrapper = ToolCallTool(call_tool=progressive._call_discovered_tool)
        # A short configured deadline keeps this real timeout test bounded;
        # production defaults are not altered or mocked.
        wrapper.card.properties["resilience"] = {"timeout_s": 1}
        wrapper_name = register(wrapper)
        calls[0] = ToolCall(
            id="first", type="function", name=wrapper_name,
            arguments=json.dumps({"name": target_name, "args": {}}),
        )
    await callbacks.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, guard)
    batch = asyncio.create_task(abilities.execute(callback_context, calls, session, parallel_tool_calls=True))
    try:
        await gate.wait_started()
        assert not await asyncio.to_thread(_file_lock_available, context.skills)
        if interruption == "wrapper_timeout":
            await asyncio.wait_for(nested_entered.wait(), timeout=5)
            await asyncio.wait_for(nested_cancelled.wait(), timeout=5)
            assert not context.route.blocked
        elif interruption == "hitl":
            await asyncio.wait_for(question_raised.wait(), timeout=5)
            await asyncio.sleep(0)
            assert not batch.done() and not context.route.blocked
        else:
            batch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(batch, timeout=1)
            assert context.route.blocked
            assert not await asyncio.to_thread(_file_lock_available, context.skills)
        gate.release.set()
        if interruption != "batch_abort":
            results = await asyncio.wait_for(batch, timeout=5)
            if interruption == "hitl":
                assert isinstance(results[0][0], ToolInterruptException)
            else:
                assert "timed out" in results[0][1].content
            assert [result[1].content for result in results[1:]] == ["done", "done"]
            assert body_calls == ["success", "success"]
            assert gate.calls == 3
            assert await context.router.is_current(context.route)
    finally:
        if not batch.done():
            batch.cancel()
        await gate.finish(batch)
        await callbacks.clear()
        for card in registered:
            abilities.remove(card.name)
            Runner.resource_mgr.remove_tool(card.id)
    assert await asyncio.to_thread(_file_lock_available, context.skills)
