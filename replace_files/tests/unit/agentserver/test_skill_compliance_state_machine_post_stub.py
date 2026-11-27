"""Post-stub form regression for SkillComplianceRail.

Background: openjiuwen ``SkillUseRail`` (priority=100) runs before this rail
on ``after_tool_call`` and rewrites the ``skill_tool`` ToolMessage:

  * ``tool_msg.content`` is replaced with a short ``[SKILL LOADED]`` stub
  * ``metadata['is_skill_body']`` is flipped to ``False``
  * ``metadata['original_is_skill_body'] = True`` is added as a marker

If SkillComplianceRail keyed only on ``is_skill_body``, it would never see
the body-load event in production and ``phase`` would stay ``IDLE`` — which
is exactly the regression these tests guard against.
"""
# pylint: disable=protected-access
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jiuwenclaw.agentserver.deep_agent.rails import skill_compliance_rail as rail_mod
from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import (
    SkillComplianceRail,
    SkillPhase,
    _SkillSessionState,
    _sessions,
)
from jiuwenclaw.agentserver.tools import todo_toolkits


@pytest.fixture(autouse=True)
def _clean_state():
    _sessions.clear()
    todo_toolkits._last_op_result.clear()
    yield
    _sessions.clear()
    todo_toolkits._last_op_result.clear()


def _set_phase(session_id, phase, skill="demo"):
    state = _SkillSessionState()
    state.phase = phase
    state.active_skill = skill if phase != SkillPhase.IDLE else None
    state.skill_bash_commands = []
    _sessions[session_id] = state
    return state


def _mk_tool_call(name, **arguments):
    tc = MagicMock()
    tc.name = name
    tc.arguments = arguments
    return tc


def _mk_tool_msg(content="", metadata=None):
    msg = MagicMock()
    msg.content = content
    msg.metadata = metadata or {}
    return msg


def _mk_ctx(session):
    """Build an AgentCallbackContext-shaped object whose
    ``context._session_ref`` matches what ``SkillUseRail`` aligns at write
    time (see openjiuwen skill_use_rail.py:493-516)."""
    return SimpleNamespace(
        context=SimpleNamespace(_session_ref=session),
        session=session,
        inputs=None,
    )


def test_activates_when_metadata_already_stubbed():
    """
    Production form: is_skill_body=False, original_is_skill_body=True,
    content stubbed. Rail must (1) still recognize this as a body-load event
    via ``original_is_skill_body``, (2) recover the full body from session
    active state for bash-block parsing, (3) append the load directive.
    """
    from openjiuwen.core.context_engine.active_skill_bodies import (
        ACTIVE_SKILL_BODIES_STATE_KEY,
        _state_key,
        normalize_skill_relative_file_path,
    )

    sid = "s-post-stub"
    rail = SkillComplianceRail()
    state = _set_phase(sid, SkillPhase.IDLE)
    state.active_skill = None

    stub_content = "[SKILL LOADED] pdf / SKILL.md\nbody kept in session state"
    tc = _mk_tool_call("skill_tool", skill_name="pdf")
    tool_msg = _mk_tool_msg(stub_content, metadata={
        "is_skill_body": False,
        "original_is_skill_body": True,
        "skill_body_stub": True,
        "skill_body_active": True,
        "skill_name": "pdf",
        "relative_file_path": "SKILL.md",
    })

    full_body = (
        "# pdf skill\n\n"
        "Stage 1\n"
        "```bash\n"
        "python build_pdf.py --in foo.md\n"
        "```\n"
    )
    key = _state_key("pdf", normalize_skill_relative_file_path("SKILL.md"))
    mock_session = MagicMock()
    mock_session.get_state.return_value = {
        key: {
            "skill_name": "pdf",
            "relative_file_path": "SKILL.md",
            "body": full_body,
        },
    }
    ctx = _mk_ctx(mock_session)

    with patch.object(rail_mod, "_read_initial_phase_from_disk",
                      return_value=SkillPhase.WAITING_PLAN):
        rail._handle_tool_event(
            state, tc, tool_msg, "skill_tool", sid, ctx=ctx,
        )

    assert state.phase == SkillPhase.WAITING_PLAN
    assert state.active_skill == "pdf"
    assert state.active_skill_content == full_body
    # Critical: bash command parsed from full body, not the stub.
    assert any("build_pdf.py" in cmd for cmd in state.skill_bash_commands)
    # Load directive appended to the stubbed tool_msg content.
    assert tool_msg.content.startswith(stub_content)
    assert len(tool_msg.content) > len(stub_content)


def test_falls_back_to_tool_msg_when_session_state_missing():
    """
    ctx.context._session_ref is None: rail still activates on the stub
    content; ``skill_bash_commands`` parses empty — equivalent to pre-fix
    production behavior, no regression.
    """
    sid = "s-post-stub-no-session"
    rail = SkillComplianceRail()
    state = _set_phase(sid, SkillPhase.IDLE)
    state.active_skill = None

    stub_content = "[SKILL LOADED] pdf / SKILL.md"
    tc = _mk_tool_call("skill_tool", skill_name="pdf")
    tool_msg = _mk_tool_msg(stub_content, metadata={
        "is_skill_body": False,
        "original_is_skill_body": True,
        "skill_name": "pdf",
        "relative_file_path": "SKILL.md",
    })
    ctx = _mk_ctx(session=None)

    with patch.object(rail_mod, "_read_initial_phase_from_disk",
                      return_value=SkillPhase.WAITING_PLAN):
        rail._handle_tool_event(
            state, tc, tool_msg, "skill_tool", sid, ctx=ctx,
        )

    assert state.phase == SkillPhase.WAITING_PLAN
    assert state.active_skill == "pdf"
    assert state.skill_bash_commands == []
    # Load directive still appended.
    assert tool_msg.content.startswith(stub_content)
    assert len(tool_msg.content) > len(stub_content)
