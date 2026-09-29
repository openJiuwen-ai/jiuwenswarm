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
