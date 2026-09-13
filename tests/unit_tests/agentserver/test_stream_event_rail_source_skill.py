# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""source_skill attribution in JiuSwarmStreamEventRail tool stream payloads."""

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.rails.skills.skill_use_rail import (
    clear_current_skill_name,
    set_current_skill_name,
)

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _StreamSession:
    """Session stub: stream chunks + get_state/update_state for skill binding."""

    def __init__(self):
        self.chunks = []
        self._state = {}

    async def write_stream(self, chunk):
        self.chunks.append(chunk)

    def get_state(self, key=None):
        if key is None:
            return dict(self._state)
        return self._state.get(key)

    def update_state(self, data: dict):
        self._state.update(data)


def _ctx(session, tool_name: str, tool_call_id: str = "call-1"):
    tool_call = SimpleNamespace(id=tool_call_id, name=tool_name, arguments={})
    return SimpleNamespace(
        session=session,
        agent=None,
        context=None,
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args={},
            tool_result=SimpleNamespace(success=True, data=None, error=""),
        ),
    )


@pytest.mark.asyncio
async def test_emit_tool_call_includes_source_skill_when_active() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "bash", tool_call_id="bash-1")
    set_current_skill_name("web-research", session=session)
    # ContextVar often empty in real tool contexts; session must still win.
    clear_current_skill_name()

    await rail.before_tool_call(ctx)

    tool_calls = [chunk for chunk in session.chunks if chunk.type == "tool_call"]
    assert len(tool_calls) == 1
    payload = tool_calls[0].payload["tool_call"]
    assert payload["name"] == "bash"
    assert payload["source_skill"] == "web-research"

    updates = [chunk for chunk in session.chunks if chunk.type == "tool_update"]
    assert updates[-1].payload["tool_update"]["source_skill"] == "web-research"


@pytest.mark.asyncio
async def test_emit_tool_call_omits_source_skill_when_unset() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "read_file", tool_call_id="read-1")
    clear_current_skill_name(session=session)

    await rail.before_tool_call(ctx)

    payload = session.chunks[0].payload["tool_call"]
    assert "source_skill" not in payload


@pytest.mark.asyncio
async def test_emit_tool_result_includes_source_skill_when_active() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "bash", tool_call_id="bash-2")
    set_current_skill_name("code-review", session=session)
    clear_current_skill_name()

    await rail.after_tool_call(ctx)

    tool_results = [chunk for chunk in session.chunks if chunk.type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].payload["tool_result"]["source_skill"] == "code-review"
