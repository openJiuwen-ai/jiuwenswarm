# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""§6 missing-test fill-in for issue 3975.

Origin labels (also recorded in 迁移方案.md §6.1.1):

- 移植: D lives in ``test_agent_manager_session_cleanup.py`` (develop
  ``e3ee2dec``). This module does not duplicate those two functions.
- 自拟: every test in this file. develop has no functions with these names.
  B is spawn→TTL evict→real resume→second input. C is two real TaskTool
  invokes with the flag off. Neither uses a live LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.subagent_stream import (
    clear_all_subagent_progress_batches,
    persist_subagent_transcript_message,
    resolve_subagent_parallel_fields,
)
from jiuwenswarm.server.runtime.session import session_history


def _make_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


class _IdleChildAdapter:
    def __init__(self) -> None:
        self.cleaned = False
        self.released: list[tuple[str, str]] = []
        self._instance = None

    @staticmethod
    def is_session_active(_session_id: str) -> bool:
        return False

    @staticmethod
    def is_deep_agent_executing_for_session(_session_id: str) -> bool:
        return False

    async def stop_interaction(self) -> None:
        return None

    async def release_subagent_runtime_for_session(
        self,
        session_id: str,
        *,
        reason: str = "",
    ) -> None:
        self.released.append((session_id, reason))

    async def cleanup(self) -> None:
        self.cleaned = True


class _FailingCleanupChildAdapter(_IdleChildAdapter):
    async def cleanup(self) -> None:
        raise RuntimeError("child cleanup failed")


@dataclass
class _MockSubagentSession:
    pre_run_calls: int = 0
    close_stream_calls: int = 0
    commit_calls: int = 0

    async def pre_run(self, **kwargs: object) -> None:
        del kwargs
        self.pre_run_calls += 1

    async def close_stream(self) -> None:
        self.close_stream_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1


@dataclass
class _MockSubagent:
    output: str = "first-turn"
    card: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(id="sub-card", name="explore")
    )

    async def stream(
        self,
        inputs: dict[str, str],
        *,
        session: _MockSubagentSession,
    ) -> AsyncIterator[dict[str, object]]:
        del inputs, session
        yield {"type": "llm_output", "payload": {"content": self.output}}
        yield {
            "type": "answer",
            "payload": {"output": self.output, "result_type": "answer"},
        }


class _ControlParent:
    def __init__(self, output: str = "first-turn") -> None:
        self.output = output
        self.created: list[_MockSubagent] = []

    def create_subagent(
        self,
        subagent_type: str,
        subsession_id: str,
        browser_capabilities: list[str] | None = None,
    ) -> _MockSubagent:
        del subagent_type, subsession_id, browser_capabilities
        agent = _MockSubagent(output=self.output)
        self.created.append(agent)
        return agent


class _SeededCheckpointer:
    def __init__(self) -> None:
        self.ids: set[str] = set()

    def seed(self, session_id: str) -> None:
        self.ids.add(session_id)

    async def session_exists(self, session_id: str) -> bool:
        return session_id in self.ids


async def _wait_spawned(control: Any, subagent_id: str) -> Any:
    from openjiuwen.harness.subagent_runtime.models import SubagentStatusKind

    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        instance = control._manager.find(subagent_id)
        if instance is not None and instance.agent_status().kind is SubagentStatusKind.COMPLETED:
            return instance
        await asyncio.sleep(0.01)
    raise TimeoutError(f"subagent {subagent_id} did not finish a turn")


def _parent_with_child(session_id: str, child: _IdleChildAdapter) -> JiuWenSwarmDeepAdapter:
    return _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={session_id: child},
        _session_adapter_locks={session_id: asyncio.Lock()},
        _session_adapter_last_used={session_id: 0.0},
        _session_adapter_versions={session_id: 1},
        _session_adapter_reload_failures={},
        SESSION_ADAPTER_EVICT_BATCH_SIZE=8,
        SESSION_ADAPTER_IDLE_TTL_SEC=1.0,
    )


@pytest.mark.asyncio
async def test_subagent_resume_after_adapter_eviction_preserves_context(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origin: 自拟 §6.B. Connected spawn → persist → TTL evict → resume → send_input.

    No live LLM. Uses CompatibleSubagentControl.spawn/close/hydrate/resume/send_input
    and dest Adapter TTL. Does not monkeypatch ``SubagentControl.resume``.
    """
    pytest.importorskip("openjiuwen.harness.subagent_runtime")
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness.subagent_runtime.models import SubagentStatusKind
    from openjiuwen.harness.subagent_runtime.persistence import read_subagent_bucket
    from openjiuwen.harness.subagent_runtime.status_events import (
        build_subagent_updated_payload,
    )
    from jiuwenswarm.agents.harness.common.tools.subagent_compat import (
        CompatibleSubagentControl,
    )

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    parent_session_id = "parent-evict-1"
    parent_session = Session(session_id=parent_session_id)
    parent_session.commit = lambda: None
    checkpointer = _SeededCheckpointer()
    runtime_config = SubagentRuntimeConfig(
        enable_activity_stream=False,
        enable_transcript_stream=False,
    )
    old_control = None
    new_control = None
    try:
        with patch(
            "openjiuwen.harness.subagent_runtime.session_manager.create_agent_session",
            side_effect=lambda **kwargs: _MockSubagentSession(),
        ), patch(
            "openjiuwen.harness.subagent_runtime.control.WAIT_TIMEOUT_MS_MIN",
            100,
        ), patch(
            "openjiuwen.harness.subagent_runtime.control.CheckpointerFactory.get_checkpointer",
            return_value=checkpointer,
        ):
            old_parent = _ControlParent(output="first-turn")
            old_control = CompatibleSubagentControl(
                old_parent,
                parent_session_id,
                config=runtime_config,
                parent_session=parent_session,
            )
            spawned = await old_control.spawn("explore", "first question")
            subagent_id = spawned.subagent_id
            persist_subagent_transcript_message(
                {
                    "parent_session_id": parent_session_id,
                    "subagent_id": subagent_id,
                    "content": "child context before eviction",
                    "role": "assistant",
                    "seq": 1,
                }
            )
            first = await _wait_spawned(old_control, subagent_id)
            waited = await old_control.wait([subagent_id], timeout_ms=500)
            assert waited.timed_out is False
            assert first.last_output == "first-turn"
            checkpointer.seed(subagent_id)
            await old_control.close(subagent_id, reason="manual")
            await old_control.persist()
            assert int(old_control.capacity().get("used", 0)) == 0
            assert subagent_id in read_subagent_bucket(parent_session)["records"]

            old_adapter = _IdleChildAdapter()
            old_adapter._instance = SimpleNamespace(
                _subagent_controls={parent_session_id: old_control},
            )
            parent = _parent_with_child(parent_session_id, old_adapter)
            await parent._evict_idle_session_adapters()
            assert old_adapter.cleaned is True
            assert parent._session_adapters == {}
            rows = session_history.load_history_records(
                parent_session_id,
                subagent_id=subagent_id,
            )
            assert any(
                row.get("content") == "child context before eviction" for row in rows
            )
            assert session_history.load_history_records(parent_session_id) == []

            new_parent = _ControlParent(output="second-turn")
            new_control = CompatibleSubagentControl(
                new_parent,
                parent_session_id,
                config=runtime_config,
                parent_session=parent_session,
            )
            assert new_control is not old_control
            new_control.hydrate()
            result = await new_control.resume(subagent_id)
            assert result.restored is True
            assert result.status.kind is SubagentStatusKind.COMPLETED
            payload = build_subagent_updated_payload(
                subagent_id=subagent_id,
                subagent_type="explore",
                display_name="Explorer",
                role="researcher",
                parent_session_id=parent_session_id,
                task_description="first question",
                created_at_ms=1.0,
                updated_at_ms=2.0,
                closed_at_ms=None,
                status=result.status,
                revision=1,
            )
            assert payload["status"] == "idle"
            assert payload["can_send_input"] is True
            assert payload["subagent_id"] == subagent_id

            await new_control.send_input(subagent_id, "second question")
            second = await _wait_spawned(new_control, subagent_id)
            second_wait = await new_control.wait([subagent_id], timeout_ms=500)
            assert second_wait.timed_out is False
            assert second.last_output == "second-turn"
            assert second.subagent_id == subagent_id

            new_adapter = _IdleChildAdapter()
            new_adapter._instance = SimpleNamespace(
                _subagent_controls={parent_session_id: new_control},
            )
            parent._session_adapters[parent_session_id] = new_adapter
            assert new_adapter is not old_adapter
            assert parent._session_adapters[parent_session_id] is not old_adapter
    finally:
        for control in (old_control, new_control):
            if control is None:
                continue
            for sid in list(control._manager.list_ids()):
                await control._manager.remove(sid, reason="test_cleanup")
                control._registry.release(sid)


@pytest.mark.asyncio
async def test_idle_eviction_skips_live_subagent_runtime() -> None:
    """Origin: 自拟 §6.B/L TTL. Live children must block Adapter eviction."""
    child = _IdleChildAdapter()
    child._instance = SimpleNamespace(
        _subagent_controls={
            "sess-live": SimpleNamespace(capacity=lambda: {"used": 2}),
        }
    )
    parent = _parent_with_child("sess-live", child)
    await parent._evict_idle_session_adapters()
    assert child.cleaned is False
    assert parent._session_adapters["sess-live"] is child


@pytest.mark.asyncio
async def test_flag_off_task_tool_remains_ephemeral_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origin: 自拟 §6.C. Two real TaskTool.invoke calls with the flag off.

    No model. Flag-off rail.init registers the real task_tool, two invokes
    create two ephemeral conversation ids, and no persistent control remains.
    """
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.rails.subagent import subagent_rail as rail_mod
    from openjiuwen.harness.tools.subagent.task_tool import TaskTool
    from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
        BrowserTaskPromptRail,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter as DeepAdapter,
    )

    persistent_inits: list[str] = []
    conversation_ids: list[str] = []

    class _FakeSub:
        card = SimpleNamespace(name="explore_agent", description="explore", id="explore")

        async def invoke(self, inputs: dict[str, str], session: object = None) -> dict[str, str]:
            del session
            conversation_ids.append(str(inputs.get("conversation_id") or ""))
            return {"output": f"done-{len(conversation_ids)}"}

    def _persistent_tools(**kwargs: object) -> list[object]:
        del kwargs
        persistent_inits.append("subagent_spawn")
        return [SimpleNamespace(card=SimpleNamespace(name="subagent_spawn"))]

    if hasattr(rail_mod, "build_subagent_tools"):
        monkeypatch.setattr(rail_mod, "build_subagent_tools", _persistent_tools)

    registered: list[object] = []
    parent_agent = SimpleNamespace(
        create_subagent=lambda *_args, **_kwargs: _FakeSub(),
        deep_config=SimpleNamespace(
            subagents=[
                SimpleNamespace(
                    card=SimpleNamespace(name="explore_agent", description="explore")
                )
            ]
        ),
        ability_manager=SimpleNamespace(
            add_ability=lambda card, tool: registered.append((card, tool))
        ),
        card=SimpleNamespace(id="parent-agent"),
        system_prompt_builder=SimpleNamespace(language="en"),
    )

    adapter = DeepAdapter()
    off_rail = adapter._build_subagent_rail({})
    assert isinstance(off_rail, BrowserTaskPromptRail)
    assert off_rail.enable_subagent_runtime is False
    off_rail.init(parent_agent)
    assert persistent_inits == []
    assert not hasattr(parent_agent, "_subagent_controls")
    tools = [tool for _card, tool in registered if isinstance(tool, TaskTool)]
    assert len(tools) == 1
    tool = tools[0]
    session = Session(session_id="parent-flag-off")
    first = await tool.invoke(
        {"subagent_type": "explore_agent", "task_description": "first"},
        session=session,
    )
    second = await tool.invoke(
        {"subagent_type": "explore_agent", "task_description": "second"},
        session=session,
    )
    assert first.success is True
    assert second.success is True
    assert first.data["output"] == "done-1"
    assert second.data["output"] == "done-2"
    assert len(conversation_ids) == 2
    assert conversation_ids[0] != conversation_ids[1]
    assert conversation_ids[0].startswith("parent-flag-off_sub_explore_agent_")
    assert persistent_inits == []
    assert not hasattr(parent_agent, "_subagent_controls")
    assert all(getattr(card, "name", "") == "task_tool" for card, _tool in registered)


@pytest.mark.asyncio
async def test_parent_cleanup_isolates_failing_session_adapter() -> None:
    """Origin: 自拟 §6.L. One child cleanup exception must not skip siblings."""
    failing = _FailingCleanupChildAdapter()
    sibling = _IdleChildAdapter()
    parent = _make_adapter(
        _is_session_scoped_adapter=False,
        _session_adapters={"sess-a": failing, "sess-b": sibling},
        _session_adapter_locks={},
        _session_adapter_last_used={},
        _session_adapter_versions={},
        _session_adapter_reload_failures={},
        _instance=None,
        _memory_reindex_task=None,
        _a2x_client=None,
        _retained_sys_operation_ids=[],
    )
    await parent.cleanup()
    assert sibling.cleaned is True
    assert parent._session_adapters == {}


@pytest.mark.asyncio
async def test_cancel_session_agent_tasks_gather_isolates_exceptions() -> None:
    """Origin: 自拟 §6.L. Real asyncio.gather(..., return_exceptions=True)."""
    adapter = _make_adapter(_session_agent_tasks={})

    async def _boom() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise RuntimeError("task cleanup boom") from None

    async def _quiet() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    boom_task = asyncio.create_task(_boom())
    quiet_task = asyncio.create_task(_quiet())
    adapter._session_agent_tasks["sess-gather"] = {boom_task, quiet_task}

    cancelled = await adapter._cancel_session_agent_tasks("sess-gather")
    assert cancelled == 2
    assert boom_task.done() is True
    assert quiet_task.done() is True
    assert adapter._session_agent_tasks.get("sess-gather") in (None, set())


def test_progress_batches_isolated_across_parent_sessions() -> None:
    """Origin: 自拟 §6.L. Same child id under two parents must not share a batch."""
    clear_all_subagent_progress_batches()
    first = resolve_subagent_parallel_fields(
        parent_session_id="parent-a",
        subagent_id="sa-shared-name",
        legacy_status="starting",
    )
    second = resolve_subagent_parallel_fields(
        parent_session_id="parent-b",
        subagent_id="sa-shared-name",
        legacy_status="starting",
    )
    assert first == (0, 1, False)
    assert second == (0, 1, False)
    third = resolve_subagent_parallel_fields(
        parent_session_id="parent-a",
        subagent_id="sa-other",
        legacy_status="starting",
    )
    assert third == (1, 2, True)
    leftover_b = resolve_subagent_parallel_fields(
        parent_session_id="parent-b",
        subagent_id="sa-shared-name",
        legacy_status="starting",
    )
    assert leftover_b == (0, 1, False)


@pytest.mark.asyncio
async def test_dual_parent_real_spawn_isolates_batches_and_ttl() -> None:
    """Origin: 自拟 §6.L live. Two parents spawn two children each via gather.

    No live LLM. Real ``CompatibleSubagentControl.spawn`` + wait. Batches and
    TTL occupancy stay isolated; idle eviction must skip both live parents.
    """
    pytest.importorskip("openjiuwen.harness.subagent_runtime")
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from jiuwenswarm.agents.harness.common.tools.subagent_compat import (
        CompatibleSubagentControl,
    )

    clear_all_subagent_progress_batches()
    runtime_config = SubagentRuntimeConfig(
        enable_activity_stream=False,
        enable_transcript_stream=False,
    )
    controls: list[CompatibleSubagentControl] = []

    async def _spawn_parent(
        session_id: str,
        output: str,
    ) -> tuple[CompatibleSubagentControl, list[str]]:
        parent_session = Session(session_id=session_id)
        parent_session.commit = lambda: None
        control = CompatibleSubagentControl(
            _ControlParent(output=output),
            session_id,
            config=runtime_config,
            parent_session=parent_session,
        )
        controls.append(control)

        async def _one(prompt: str) -> str:
            spawned = await control.spawn("explore", prompt)
            resolve_subagent_parallel_fields(
                parent_session_id=session_id,
                subagent_id=spawned.subagent_id,
                legacy_status="starting",
            )
            await _wait_spawned(control, spawned.subagent_id)
            return spawned.subagent_id

        child_ids = await asyncio.gather(_one("q1"), _one("q2"))
        return control, list(child_ids)

    try:
        with patch(
            "openjiuwen.harness.subagent_runtime.session_manager.create_agent_session",
            side_effect=lambda **kwargs: _MockSubagentSession(),
        ), patch(
            "openjiuwen.harness.subagent_runtime.control.WAIT_TIMEOUT_MS_MIN",
            100,
        ):
            (control_a, ids_a), (control_b, ids_b) = await asyncio.gather(
                _spawn_parent("parent-l-a", "a-out"),
                _spawn_parent("parent-l-b", "b-out"),
            )
        ids = ids_a + ids_b
        assert len(set(ids)) == 4
        leftover_a = resolve_subagent_parallel_fields(
            parent_session_id="parent-l-a",
            subagent_id=ids_a[0],
            legacy_status="starting",
        )
        leftover_b = resolve_subagent_parallel_fields(
            parent_session_id="parent-l-b",
            subagent_id=ids_b[0],
            legacy_status="starting",
        )
        assert leftover_a[1] == 2
        assert leftover_b[1] == 2
        assert leftover_a[2] is True
        assert leftover_b[2] is True
        assert int(control_a.capacity().get("used", 0)) >= 1
        assert int(control_b.capacity().get("used", 0)) >= 1

        child_a = _IdleChildAdapter()
        child_a._instance = SimpleNamespace(
            _subagent_controls={"parent-l-a": control_a},
        )
        child_b = _IdleChildAdapter()
        child_b._instance = SimpleNamespace(
            _subagent_controls={"parent-l-b": control_b},
        )
        parent = _make_adapter(
            _is_session_scoped_adapter=False,
            _session_adapters={"parent-l-a": child_a, "parent-l-b": child_b},
            _session_adapter_locks={
                "parent-l-a": asyncio.Lock(),
                "parent-l-b": asyncio.Lock(),
            },
            _session_adapter_last_used={"parent-l-a": 0.0, "parent-l-b": 0.0},
            _session_adapter_versions={"parent-l-a": 1, "parent-l-b": 1},
            _session_adapter_reload_failures={},
            SESSION_ADAPTER_EVICT_BATCH_SIZE=8,
            SESSION_ADAPTER_IDLE_TTL_SEC=1.0,
        )
        await parent._evict_idle_session_adapters()
        assert child_a.cleaned is False
        assert child_b.cleaned is False
        assert parent._session_adapters["parent-l-a"] is child_a
        assert parent._session_adapters["parent-l-b"] is child_b
    finally:
        for control in controls:
            for sid in list(control._manager.list_ids()):
                await control._manager.remove(sid, reason="test_cleanup")
                control._registry.release(sid)
