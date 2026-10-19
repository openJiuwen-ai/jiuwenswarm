# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for OpenJiuWen subagent runtime compatibility."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("openjiuwen.harness.subagent_runtime")

from openjiuwen.harness.subagent_runtime.control import SubagentControl
from openjiuwen.harness.subagent_runtime.models import (
    ResumeResult,
    SubagentStatus,
    SubagentStatusKind,
)
from openjiuwen.harness.subagent_runtime.status import StatusChannel
from openjiuwen.harness.subagent_runtime.status_events import (
    build_subagent_updated_payload,
)

from jiuwenswarm.agents.harness.common.tools.subagent_compat import (
    CompatibleSubagentControl,
    install_subagent_control_compat_patch,
)


class _RestoredInstance:
    def __init__(self) -> None:
        self.status = StatusChannel()

    def agent_status(self) -> SubagentStatus:
        return self.status.current()


@pytest.mark.asyncio
async def test_restored_subagent_is_reported_idle_and_accepts_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed instance has no queued turn and must be externally idle."""
    restored = _RestoredInstance()
    control = object.__new__(CompatibleSubagentControl)
    setattr(control, "_manager", SimpleNamespace(find=lambda subagent_id: restored))

    async def _resume(_self: SubagentControl, subagent_id: str) -> ResumeResult:
        assert subagent_id == "sub-1"
        return ResumeResult(status=SubagentStatus.pending_init(), restored=True)

    monkeypatch.setattr(SubagentControl, "resume", _resume)

    result = await control.resume("sub-1")

    assert result.status.kind is SubagentStatusKind.COMPLETED
    payload = build_subagent_updated_payload(
        subagent_id="sub-1",
        subagent_type="explore_agent",
        display_name="Explorer",
        role="Explore code",
        parent_session_id="parent-1",
        task_description="Inspect the repository",
        created_at_ms=1.0,
        updated_at_ms=2.0,
        closed_at_ms=None,
        status=restored.agent_status(),
        revision=restored.status.version(),
    )
    assert payload["status"] == "idle"
    assert payload["can_send_input"] is True
    assert payload["needs_resume"] is False


def test_install_patch_rebinds_control_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """New parent sessions must be created with the compatible control."""
    from openjiuwen.harness.tools.subagent import _control_registry

    monkeypatch.setattr(_control_registry, "SubagentControl", SubagentControl)
    previous = CompatibleSubagentControl.mirror_child_stream
    CompatibleSubagentControl.mirror_child_stream = False

    install_subagent_control_compat_patch(mirror_child_stream=True)

    assert _control_registry.SubagentControl is CompatibleSubagentControl
    assert CompatibleSubagentControl.mirror_child_stream is True
    CompatibleSubagentControl.mirror_child_stream = previous


class _Chunk:
    def __init__(self, chunk_type: str, payload: object) -> None:
        self.type = chunk_type
        self.payload = payload


class _ParentSession:
    def __init__(self, *, fail: bool = False) -> None:
        self.written: list[tuple[str, dict]] = []
        self._fail = fail

    async def write_stream(self, schema: object) -> None:
        if self._fail:
            raise RuntimeError("stream closed")
        self.written.append((schema.type, schema.payload))


def _mirror_control(session: _ParentSession) -> CompatibleSubagentControl:
    control = object.__new__(CompatibleSubagentControl)
    control._parent_session = session  # pylint: disable=protected-access
    control._mirror_reasoning_text = {}  # pylint: disable=protected-access
    control._mirror_reasoning_flushed_at = {}  # pylint: disable=protected-access
    control._mirror_reasoning_frames = {}  # pylint: disable=protected-access
    control.mirror_child_stream = True
    return control


@pytest.mark.asyncio
async def test_mirror_replays_allowed_chunks_with_source_id() -> None:
    session = _ParentSession()
    control = _mirror_control(session)

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "先看一下"}))
    await control._on_child_chunk(
        "sub-1",
        _Chunk("tool_call", {"tool_call": {"id": "c1", "name": "web_search"}, "task_id": "t-child"}),
    )
    await control._on_child_chunk("sub-1", _Chunk("answer", {"output": "结论"}))
    await control._on_child_chunk("sub-1", _Chunk("content_chunk", {"content": "正文"}))
    await control._on_child_chunk("sub-1", _Chunk("error", {"error": "boom"}))
    await control._on_child_chunk("sub-1", _Chunk("task.start", {"task_id": "t-child"}))

    assert [item[0] for item in session.written] == ["llm_reasoning", "tool_call"]
    reasoning, tool = (item[1] for item in session.written)
    assert reasoning == {"content": "先看一下", "stream_source_id": "sub-1"}
    assert tool["stream_source_id"] == "sub-1"
    assert "task_id" not in tool
    assert tool["tool_call"]["name"] == "web_search"


@pytest.mark.asyncio
async def test_mirror_disabled_writes_nothing() -> None:
    session = _ParentSession()
    control = _mirror_control(session)
    control.mirror_child_stream = False

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "x"}))

    assert session.written == []


@pytest.mark.asyncio
async def test_mirror_write_failure_is_swallowed() -> None:
    control = _mirror_control(_ParentSession(fail=True))

    await control._on_child_chunk("sub-1", _Chunk("tool_result", {"result": "ok"}))


@pytest.mark.asyncio
async def test_reasoning_stays_buffered_until_the_next_frame() -> None:
    session = _ParentSession()
    control = _mirror_control(session)

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"output": "先"}))
    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "看"}))
    assert session.written == []

    await control._on_child_chunk("sub-1", _Chunk("tool_update", {"tool_update": {"status": "running"}}))
    await control._on_child_chunk(
        "sub-1",
        _Chunk("tool_result", {"result": "全文结果", "task_id": "child-task"}),
    )

    assert [item[0] for item in session.written] == ["llm_reasoning", "tool_result"]
    assert session.written[0][1] == {"content": "先看", "stream_source_id": "sub-1"}
    assert session.written[1][1]["result"] == "全文结果"
    assert "task_id" not in session.written[1][1]


@pytest.mark.asyncio
async def test_reasoning_flushes_at_char_threshold_and_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ParentSession()
    control = _mirror_control(session)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.subagent_compat.time.monotonic",
        lambda: clock["now"],
    )

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "a" * 200}))
    assert session.written[0][1]["content"] == "a" * 200

    await control._on_child_chunk("sub-2", _Chunk("llm_reasoning", {"content": "短"}))
    clock["now"] += 0.311
    await control._on_child_chunk("sub-2", _Chunk("llm_reasoning", {"content": "句"}))

    flushed = [item[1]["content"] for item in session.written if item[0] == "llm_reasoning"]
    assert flushed == ["a" * 200, "短句"]
    assert session.written[-1][1]["stream_source_id"] == "sub-2"


@pytest.mark.asyncio
async def test_reasoning_frame_cap_keeps_tool_frames() -> None:
    session = _ParentSession()
    control = _mirror_control(session)
    control._mirror_reasoning_frames["sub-1"] = 2000  # pylint: disable=protected-access

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "不再上屏"}))
    await control._on_child_chunk("sub-1", _Chunk("tool_call", {"tool_call": {"name": "bash"}}))

    assert [item[0] for item in session.written] == ["tool_call"]


def _patch_parent_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep lifecycle hooks from touching a control built with ``object.__new__``."""

    async def _status(_self: object, _subagent_id: str, _status: SubagentStatus) -> None:
        """Parent bookkeeping is not under test."""

    async def _evict(_self: object, _subagent_id: str, *, reason: str, persist: bool = True) -> None:
        """Parent eviction is not under test."""
        del reason, persist

    monkeypatch.setattr(SubagentControl, "_handle_instance_status_changed", _status)
    monkeypatch.setattr(SubagentControl, "_evict_from_memory", _evict)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        SubagentStatus.completed(),
        SubagentStatus.interrupted(),
        SubagentStatus.errored("boom"),
    ],
)
async def test_turn_end_flushes_short_reasoning_without_leaking(
    status: SubagentStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short tail must ship when the turn ends, and must not prefix the next turn."""
    session = _ParentSession()
    control = _mirror_control(session)
    _patch_parent_lifecycle(monkeypatch)

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "上一轮尾巴"}))
    assert session.written == []

    await control._handle_instance_status_changed("sub-1", status)

    assert session.written == [("llm_reasoning", {"content": "上一轮尾巴", "stream_source_id": "sub-1"})]
    assert "sub-1" not in control._mirror_reasoning_text  # pylint: disable=protected-access
    assert control._mirror_reasoning_frames["sub-1"] == 1  # pylint: disable=protected-access

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "新一轮"}))
    await control._on_child_chunk("sub-1", _Chunk("tool_call", {"tool_call": {"name": "bash"}}))

    reasoning = [item[1]["content"] for item in session.written if item[0] == "llm_reasoning"]
    assert reasoning == ["上一轮尾巴", "新一轮"]


@pytest.mark.asyncio
async def test_running_status_keeps_short_reasoning_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ParentSession()
    control = _mirror_control(session)
    _patch_parent_lifecycle(monkeypatch)

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "还在想"}))
    await control._handle_instance_status_changed("sub-1", SubagentStatus.running())

    assert session.written == []
    assert control._mirror_reasoning_text["sub-1"] == "还在想"  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_evict_flushes_tail_and_drops_mirror_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close/cancel both evict; the parent session must not keep that child's buffers."""
    session = _ParentSession()
    control = _mirror_control(session)
    _patch_parent_lifecycle(monkeypatch)
    control._mirror_reasoning_frames["sub-1"] = 3  # pylint: disable=protected-access

    await control._on_child_chunk("sub-1", _Chunk("llm_reasoning", {"content": "关闭前"}))
    await control._evict_from_memory("sub-1", reason="manual")

    assert session.written == [("llm_reasoning", {"content": "关闭前", "stream_source_id": "sub-1"})]
    assert "sub-1" not in control._mirror_reasoning_text  # pylint: disable=protected-access
    assert "sub-1" not in control._mirror_reasoning_flushed_at  # pylint: disable=protected-access
    assert "sub-1" not in control._mirror_reasoning_frames  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_evict_drops_mirror_state_when_parent_evict_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ParentSession()
    control = _mirror_control(session)

    async def _boom(_self: object, _subagent_id: str, *, reason: str, persist: bool = True) -> None:
        del reason, persist
        raise RuntimeError("remove failed")

    monkeypatch.setattr(SubagentControl, "_evict_from_memory", _boom)
    control._mirror_reasoning_text["sub-1"] = "尾巴"  # pylint: disable=protected-access
    control._mirror_reasoning_flushed_at["sub-1"] = 1.0  # pylint: disable=protected-access
    control._mirror_reasoning_frames["sub-1"] = 2  # pylint: disable=protected-access

    with pytest.raises(RuntimeError, match="remove failed"):
        await control._evict_from_memory("sub-1", reason="manual")

    assert session.written[0][1]["content"] == "尾巴"
    assert "sub-1" not in control._mirror_reasoning_text  # pylint: disable=protected-access
    assert "sub-1" not in control._mirror_reasoning_flushed_at  # pylint: disable=protected-access
    assert "sub-1" not in control._mirror_reasoning_frames  # pylint: disable=protected-access
