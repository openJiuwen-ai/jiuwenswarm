"""Parametrized state-machine tests for SkillComplianceRail.

Covers the 9-event × 4-phase transition matrix plus orthogonal concerns
(step-skip detection, mcp_exec_command failure recovery, process restart,
skill_complete file cleanup, and SkillProtocolPromptRail phase→section mapping).
"""
# pylint: disable=protected-access,too-many-arguments

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, Optional
from unittest.mock import MagicMock, patch

import pytest

from jiuwenclaw.agentserver.deep_agent.rails import skill_compliance_rail as rail_mod
from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import (
    SkillComplianceRail,
    SkillPhase,
    _SkillSessionState,
    _sessions,
    get_session_active_skill,
    get_session_phase,
)
from jiuwenclaw.agentserver.tools import skill_step_toolkit, todo_toolkits
from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
from jiuwenclaw.agentserver.tools.todo_toolkits import (
    TodoOpKind,
    TodoOpResult,
    consume_last_op_result,
)


# ---------------- shared helpers --------------------------------------------

@pytest.fixture(autouse=True)
def _clean_state():
    """Ensure no inter-test bleed in the module-level dicts."""
    _sessions.clear()
    todo_toolkits._last_op_result.clear()
    yield
    _sessions.clear()
    todo_toolkits._last_op_result.clear()


def _mk_tool_call(name: str, **arguments):
    tc = MagicMock()
    tc.name = name
    tc.arguments = arguments
    return tc


def _mk_tool_msg(content: str = "", metadata: Optional[dict] = None):
    msg = MagicMock()
    msg.content = content
    msg.metadata = metadata or {}
    return msg


def _set_phase(session_id: str, phase: SkillPhase, skill: str = "demo") -> _SkillSessionState:
    state = _SkillSessionState()
    state.phase = phase
    state.active_skill = skill if phase != SkillPhase.IDLE else None
    state.skill_bash_commands = []
    _sessions[session_id] = state
    return state


def _publish(session_id: str, kind: TodoOpKind, *, success: bool = True,
             remaining: int = 0, total: int = 0):
    todo_toolkits._publish_op_result(session_id, TodoOpResult(
        kind=kind, success=success, message="msg",
        remaining_count=remaining, total_count=total,
        all_completed=(success and total > 0 and remaining == 0),
    ))


def _run(coro):
    asyncio.get_event_loop()
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------- E1 / E2: skill_tool body load -----------------------------

@pytest.mark.parametrize("disk_phase", [SkillPhase.WAITING_PLAN,
                                        SkillPhase.IN_PROGRESS,
                                        SkillPhase.DONE])
def test_skill_tool_load_from_idle_lands_in_disk_phase(disk_phase):
    sid = "s-load-idle"
    rail = SkillComplianceRail()
    state = _set_phase(sid, SkillPhase.IDLE)
    state.active_skill = None  # IDLE invariant

    tc = _mk_tool_call("skill_tool", skill_name="pdf")
    tool_msg = _mk_tool_msg("BODY", metadata={"is_skill_body": True, "skill_name": "pdf"})

    with patch.object(rail_mod, "_read_initial_phase_from_disk", return_value=disk_phase):
        rail._handle_tool_event(state, tc, tool_msg, "skill_tool", sid)

    assert state.phase == disk_phase
    assert state.active_skill == "pdf"
    # Directive appended exactly once
    assert "BODY" in tool_msg.content
    assert len(tool_msg.content) > len("BODY")


def test_skill_tool_load_without_is_skill_body_is_noop():
    sid = "s-no-body"
    rail = SkillComplianceRail()
    state = _set_phase(sid, SkillPhase.IDLE)
    state.active_skill = None

    tc = _mk_tool_call("skill_tool", skill_name="pdf")
    tool_msg = _mk_tool_msg("not a body", metadata={})  # no is_skill_body
    rail._handle_tool_event(state, tc, tool_msg, "skill_tool", sid)

    assert state.phase == SkillPhase.IDLE
    assert state.active_skill is None
    assert tool_msg.content == "not a body"


def test_skill_tool_load_from_inprogress_resets_to_disk_phase():
    """Mid-skill switching to a different SKILL.md re-reads disk."""
    sid = "s-switch"
    rail = SkillComplianceRail()
    state = _set_phase(sid, SkillPhase.IN_PROGRESS, skill="old")

    tc = _mk_tool_call("skill_tool", skill_name="new")
    tool_msg = _mk_tool_msg("NEW_BODY", metadata={"is_skill_body": True, "skill_name": "new"})
    with patch.object(rail_mod, "_read_initial_phase_from_disk",
                      return_value=SkillPhase.WAITING_PLAN):
        rail._handle_tool_event(state, tc, tool_msg, "skill_tool", sid)

    assert state.active_skill == "new"
    assert state.phase == SkillPhase.WAITING_PLAN


# ---------------- E3 / E6 / E7 / E5 / E4: TodoOpResult dispatch -------------

class OpCase(NamedTuple):
    start: str
    kind: TodoOpKind
    success: bool
    remaining: int
    total: int
    all_done: bool
    expect_phase: SkillPhase
    expect_substr: Optional[str]


_OP_MATRIX = [
    # CREATE
    OpCase("waiting_plan", TodoOpKind.CREATE, True, 3, 3, False,
           SkillPhase.IN_PROGRESS, "计划已建立"),
    OpCase("waiting_plan", TodoOpKind.CREATE, False, 0, 0, False,
           SkillPhase.WAITING_PLAN, None),  # failure: no transition
    OpCase("in_progress", TodoOpKind.CREATE, True, 3, 3, False,
           SkillPhase.IN_PROGRESS, None),   # already in_progress: no transition

    # COMPLETE
    OpCase("in_progress", TodoOpKind.COMPLETE, True, 1, 3, False,
           SkillPhase.IN_PROGRESS, None),
    OpCase("in_progress", TodoOpKind.COMPLETE, True, 0, 3, True,
           SkillPhase.DONE, "全部完成"),
    OpCase("in_progress", TodoOpKind.COMPLETE, False, 0, 0, False,
           SkillPhase.IN_PROGRESS, None),

    # REMOVE
    OpCase("in_progress", TodoOpKind.REMOVE, True, 2, 2, False,
           SkillPhase.IN_PROGRESS, None),
    OpCase("in_progress", TodoOpKind.REMOVE, True, 0, 0, False,
           SkillPhase.WAITING_PLAN, "计划已清空"),

    # INSERT / START — never transition
    OpCase("in_progress", TodoOpKind.INSERT, True, 5, 5, False,
           SkillPhase.IN_PROGRESS, None),
    OpCase("in_progress", TodoOpKind.START, True, 3, 3, False,
           SkillPhase.IN_PROGRESS, None),

    # IDLE blocks all op-driven transitions
    OpCase("idle", TodoOpKind.CREATE, True, 3, 3, False,
           SkillPhase.IDLE, None),
    OpCase("idle", TodoOpKind.COMPLETE, True, 0, 3, True,
           SkillPhase.IDLE, None),
]


@pytest.mark.parametrize("case", _OP_MATRIX)
def test_op_result_dispatch(case):
    sid = f"s-op-{case.kind.value}-{case.start}-{case.remaining}-{case.total}-{case.all_done}"
    start_phase = SkillPhase(case.start)
    state = _set_phase(sid, start_phase)
    rail = SkillComplianceRail()

    _publish(sid, case.kind, success=case.success,
             remaining=case.remaining, total=case.total)

    tool_msg = _mk_tool_msg("ORIG")
    rail._handle_tool_event(
        state,
        _mk_tool_call("skill_step", action=case.kind.value, idx=1),
        tool_msg, "skill_step", sid,
    )

    assert state.phase == case.expect_phase, f"phase mismatch for {case.kind.value}@{case.start}"
    if case.expect_substr is None:
        assert tool_msg.content == "ORIG", (
            f"unexpected directive on {case.kind.value}@{case.start}"
        )
    else:
        assert case.expect_substr in tool_msg.content, (
            f"expected '{case.expect_substr}' in directive, got: {tool_msg.content[-200:]}"
        )


def test_skill_step_list_is_pure_noop():
    sid = "s-list"
    state = _set_phase(sid, SkillPhase.IN_PROGRESS)
    rail = SkillComplianceRail()
    # toolkit's todo_list does NOT publish; consume_last_op_result returns None
    assert consume_last_op_result(sid) is None
    tool_msg = _mk_tool_msg("LIST OUTPUT")
    rail._handle_tool_event(
        state, _mk_tool_call("skill_step", action="list"),
        tool_msg, "skill_step", sid,
    )
    assert state.phase == SkillPhase.IN_PROGRESS
    assert tool_msg.content == "LIST OUTPUT"


# ---------------- E9: skill_complete ----------------------------------------

@pytest.mark.parametrize("start_phase", [
    SkillPhase.WAITING_PLAN, SkillPhase.IN_PROGRESS, SkillPhase.DONE,
])
def test_skill_complete_returns_to_idle_and_clears_file(start_phase):
    sid = "s-complete"
    with tempfile.TemporaryDirectory() as tmp:
        # Pre-populate file via toolkit so clear_tasks has something to remove
        SkillStepToolkit(session_id=sid, todo_dir=Path(tmp)).todo_create(["a", "b"])
        consume_last_op_result(sid)

        state = _set_phase(sid, start_phase, skill="pdf")
        rail = SkillComplianceRail()

        tc = _mk_tool_call("skill_complete", skill_name="pdf")
        tool_msg = _mk_tool_msg("done")
        with patch.object(
            skill_step_toolkit, "SkillStepToolkit",
            lambda session_id=None: SkillStepToolkit(session_id=session_id, todo_dir=Path(tmp)),
        ):
            rail._handle_tool_event(state, tc, tool_msg, "skill_complete", sid)

        assert state.phase == SkillPhase.IDLE
        assert state.active_skill is None
        assert not (Path(tmp) / "skill_step.md").exists()


def test_skill_complete_for_wrong_skill_is_noop():
    sid = "s-wrong-skill"
    state = _set_phase(sid, SkillPhase.IN_PROGRESS, skill="pdf")
    rail = SkillComplianceRail()
    tc = _mk_tool_call("skill_complete", skill_name="other")
    rail._handle_tool_event(state, tc, _mk_tool_msg(""), "skill_complete", sid)
    assert state.phase == SkillPhase.IN_PROGRESS
    assert state.active_skill == "pdf"


# ---------------- non-skill_step tool: rail does nothing structurally -------

def test_non_skill_step_tool_does_not_change_phase_or_directive():
    sid = "s-passive"
    state = _set_phase(sid, SkillPhase.IN_PROGRESS)
    rail = SkillComplianceRail()
    tool_msg = _mk_tool_msg("OUTPUT")
    rail._handle_tool_event(state, _mk_tool_call("read_file"), tool_msg, "read_file", sid)
    assert state.phase == SkillPhase.IN_PROGRESS
    assert tool_msg.content == "OUTPUT"


# ---------------- get_session_phase / get_session_active_skill --------------

def test_public_accessors_track_state():
    sid = "s-access"
    assert get_session_phase(sid) == SkillPhase.IDLE
    assert get_session_active_skill(sid) is None
    _set_phase(sid, SkillPhase.WAITING_PLAN, skill="pdf")
    assert get_session_phase(sid) == SkillPhase.WAITING_PLAN
    assert get_session_active_skill(sid) == "pdf"


# ---------------- step-skip detection (orthogonal) --------------------------

def test_step_skip_warns_then_blocks():
    sid = "s-skip"
    state = _set_phase(sid, SkillPhase.IN_PROGRESS)
    rail = SkillComplianceRail()
    # First declare Stage 1
    rail._check_step_skip(state, "[当前步骤: Stage 1] start")
    assert state.last_declared_step == 1
    assert state.pending_skip_warning is None
    # Skip to Stage 3 → warned
    rail._check_step_skip(state, "[当前步骤: Stage 3] jumped")
    assert state.skip_warned is True
    assert state.pending_skip_warning is not None
    assert "Stage 2" in state.pending_skip_warning
    # Try again to skip even further → blocked
    state.pending_skip_warning = None
    state.last_declared_step = 1
    state.skip_warned = True
    rail._check_step_skip(state, "[当前步骤: Stage 4] still jumping")
    assert "拦截" in state.pending_skip_warning


def test_step_skip_idle_phase_is_noop():
    sid = "s-idle-skip"
    state = _set_phase(sid, SkillPhase.IDLE)
    state.active_skill = None
    rail = SkillComplianceRail()
    rail._check_step_skip(state, "[当前步骤: Stage 5] random")
    assert state.last_declared_step is None
    assert state.pending_skip_warning is None


# ---------------- script failure recovery (orthogonal) ----------------------

def test_script_failure_recovery_appends_directive():
    sid = "s-script"
    state = _set_phase(sid, SkillPhase.IN_PROGRESS)
    state.skill_bash_commands = ["python build_pptx.py --in foo.md"]
    rail = SkillComplianceRail()
    tc = _mk_tool_call("mcp_exec_command", command="python build_pptx.py --in foo.md")
    tool_msg = _mk_tool_msg('{"exit_code": 1, "stderr": "ModuleNotFoundError: pptx"}')
    rail._detect_script_failure(state, tc, tool_msg)
    assert "脚本执行失败" in tool_msg.content or "Script Failure" in tool_msg.content
    assert "build_pptx.py" in tool_msg.content


def test_script_failure_recovery_idle_is_noop():
    sid = "s-script-idle"
    state = _set_phase(sid, SkillPhase.IDLE)
    rail = SkillComplianceRail()
    tc = _mk_tool_call("mcp_exec_command", command="python x.py")
    tool_msg = _mk_tool_msg('ModuleNotFoundError')
    rail._detect_script_failure(state, tc, tool_msg)
    assert "脚本执行失败" not in tool_msg.content
    assert "Script Failure" not in tool_msg.content


# ---------------- process-restart simulation --------------------------------

def test_process_restart_recovers_phase_via_disk_read():
    sid = "s-restart"
    with tempfile.TemporaryDirectory() as tmp:
        # Pre-create plan on disk (simulates state from previous process)
        SkillStepToolkit(session_id=sid, todo_dir=Path(tmp)).todo_create(["a", "b"])
        consume_last_op_result(sid)

        # _sessions is empty (process just restarted)
        assert get_session_phase(sid) == SkillPhase.IDLE

        # Model reloads SKILL.md → activate path reads disk
        rail = SkillComplianceRail()
        state = _set_phase(sid, SkillPhase.IDLE)
        state.active_skill = None

        with patch.object(
            skill_step_toolkit, "SkillStepToolkit",
            lambda session_id=None: SkillStepToolkit(session_id=session_id, todo_dir=Path(tmp)),
        ):
            tc = _mk_tool_call("skill_tool", skill_name="pdf")
            tool_msg = _mk_tool_msg("BODY", metadata={"is_skill_body": True, "skill_name": "pdf"})
            rail._handle_tool_event(state, tc, tool_msg, "skill_tool", sid)

        # Should land in IN_PROGRESS (recovered from disk)
        assert state.phase == SkillPhase.IN_PROGRESS
        assert state.active_skill == "pdf"


# ---------------- end-to-end: full skill lifecycle --------------------------

def test_full_lifecycle_idle_to_done_to_idle():
    sid = "s-lifecycle"
    with tempfile.TemporaryDirectory() as tmp:
        rail = SkillComplianceRail()
        state = _set_phase(sid, SkillPhase.IDLE)
        state.active_skill = None

        with patch.object(
            skill_step_toolkit, "SkillStepToolkit",
            lambda session_id=None: SkillStepToolkit(session_id=session_id, todo_dir=Path(tmp)),
        ):
            # 1. Load SKILL.md (no prior plan)
            tc1 = _mk_tool_call("skill_tool", skill_name="pdf")
            msg1 = _mk_tool_msg("BODY", metadata={"is_skill_body": True, "skill_name": "pdf"})
            rail._handle_tool_event(state, tc1, msg1, "skill_tool", sid)
            assert state.phase == SkillPhase.WAITING_PLAN

            # 2. Create plan
            tk = SkillStepToolkit(session_id=sid, todo_dir=Path(tmp))
            tk.todo_create(["s1", "s2"])
            tc2 = _mk_tool_call("skill_step", action="create", tasks=["s1", "s2"])
            msg2 = _mk_tool_msg("created")
            rail._handle_tool_event(state, tc2, msg2, "skill_step", sid)
            assert state.phase == SkillPhase.IN_PROGRESS

            # 3. Complete each step
            tk.todo_complete(1, "ok")
            rail._handle_tool_event(
                state, _mk_tool_call("skill_step", action="complete", idx=1),
                _mk_tool_msg("c1"), "skill_step", sid,
            )
            assert state.phase == SkillPhase.IN_PROGRESS

            tk.todo_complete(2, "ok")
            rail._handle_tool_event(
                state, _mk_tool_call("skill_step", action="complete", idx=2),
                _mk_tool_msg("c2"), "skill_step", sid,
            )
            assert state.phase == SkillPhase.DONE

            # 4. skill_complete
            rail._handle_tool_event(
                state, _mk_tool_call("skill_complete", skill_name="pdf"),
                _mk_tool_msg("done"), "skill_complete", sid,
            )
            assert state.phase == SkillPhase.IDLE
            assert state.active_skill is None
            assert not (Path(tmp) / "skill_step.md").exists()


# ---------------- SkillProtocolPromptRail phase → section mapping -----------

@pytest.mark.parametrize("phase,expect_plan_action,expect_complete_action", [
    (SkillPhase.IDLE, "remove", "remove"),
    (SkillPhase.WAITING_PLAN, "add", "remove"),
    (SkillPhase.IN_PROGRESS, "remove", "remove"),
    (SkillPhase.DONE, "remove", "add"),
])
def test_prompt_rail_phase_to_section_mapping(
    phase, expect_plan_action, expect_complete_action,
):
    from jiuwenclaw.agentserver.deep_agent.rails.skill_prompt_rail import (
        SkillProtocolPromptRail,
    )

    sid = "s-prompt"
    state = _set_phase(sid, phase, skill="pdf")
    if phase == SkillPhase.IDLE:
        state.active_skill = None  # IDLE invariant

    def _no_section(name):
        return None

    rail = SkillProtocolPromptRail()
    rail.system_prompt_builder = MagicMock()
    rail.system_prompt_builder.language = "cn"
    rail.system_prompt_builder.get_section = _no_section

    actions: dict[str, str] = {}

    def add(section):
        actions[section.name] = "add"

    def remove(name):
        actions[name] = "remove"

    rail.system_prompt_builder.add_section = add
    rail.system_prompt_builder.remove_section = remove

    rail._refresh_skill_phase_sections("cn", sid)

    assert actions.get("skill_plan_required") == expect_plan_action
    assert actions.get("skill_complete_required") == expect_complete_action


# ---------------- todo_path multi-tenant verification -----------------------

def test_resolve_todo_file_path_uses_multi_tenant_sessions_dir():
    """验证 _resolve_todo_file_path 使用多租户 sessions 目录。"""
    from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import (
        _resolve_todo_file_path,
    )
    
    session_id = "test_session_001"
    
    # Mock get_agent_sessions_dir 返回多租户路径
    expected_sessions_dir = Path(
        "/tmp/test_jiuwenclaw/service_test_service/agent_test_agent/agent/sessions"
    )
    expected_todo_path = expected_sessions_dir / session_id / "skill_step.md"
    
    with patch(
        "jiuwenclaw.agentserver.tools.todo_toolkits.get_agent_sessions_dir",
        return_value=expected_sessions_dir,
    ):
        result = _resolve_todo_file_path(session_id)
    
    # 验证返回的路径包含多租户特征
    assert result is not None, "todo_path should not be None"
    assert "service_test_service" in result, f"Expected service ID in path: {result}"
    assert "agent_test_agent" in result, f"Expected agent ID in path: {result}"
    assert session_id in result, f"Expected session ID in path: {result}"
    assert "skill_step.md" in result, f"Expected skill_step.md in path: {result}"
    
    # 验证完整路径
    assert result == str(expected_todo_path), f"Path mismatch: {result} != {expected_todo_path}"
