# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""run_tests evidence in the implementation state machine and Michael's rails."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
    UserMessageInputs,
)

from jiuwenswarm.agents.harness.code.implementation_state import (
    ImplementationStateMachine,
)
from jiuwenswarm.agents.harness.code.rails.implement_completeness_rail import (
    ImplementCompletenessRail,
)

try:
    from jiuwenswarm.agents.harness.code.rails.code_execution_discipline_rail import (
        CodeExecutionDisciplineRail,
    )
except ModuleNotFoundError:  # prep stack A lands before coding-policy rail (B)
    CodeExecutionDisciplineRail = None  # type: ignore[misc, assignment]

from .conftest import write


class _Builder:
    def __init__(self):
        self.sections = {}

    def add_section(self, section):
        self.sections[section.name] = section

    def remove_section(self, name):
        self.sections.pop(name, None)


def _ctx(agent, inputs):
    return AgentCallbackContext(agent=agent, inputs=inputs, session=SimpleNamespace(session_id="s1"))


def test_state_machine_accepts_structured_evidence():
    sm = ImplementationStateMachine()
    sm.start_turn("Fix the parser")
    sm.record_mutation("pkg/parser.py", "modify", edited_existing=True)
    sm.record_verification_evidence(["python"], True)
    assert sm.completion_status().complete
    sm.record_mutation("pkg/parser.py", "modify", edited_existing=True)
    assert sm.completion_status().state == "stale_verification"
    sm.record_verification_evidence(["python"], False)
    assert sm.completion_status().state == "verification_failed"
    sm.record_mutation("web/app.ts", "modify", edited_existing=True)
    sm.record_verification_evidence(["python"], True)
    assert sm.completion_status().state == "wrong_verification"



@pytest.mark.asyncio
async def test_completeness_rail_records_run_tests_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.implement_completeness_rail.default_mutation_store_root",
        lambda session_id: tmp_path / "ledger" / session_id,
    )
    project = tmp_path / "project"
    target = write(project, "parser.py", "value = 1\n")
    agent = SimpleNamespace(system_prompt_builder=_Builder(), _jiuwenswarm_code_project_dir=str(project))
    rail = ImplementCompletenessRail()
    rail.init(agent)
    await rail.on_user_message(_ctx(agent, UserMessageInputs(parts=["Fix the parser"], source="query")))
    edit = ToolCallInputs(tool_call=SimpleNamespace(id="e1"), tool_name="edit_file",
                          tool_args={"path": str(target), "old_string": "value = 1", "new_string": "value = parse()"})
    await rail.before_tool_call(_ctx(agent, edit))
    target.write_text("value = parse()\n")
    edit.tool_result = SimpleNamespace(content="updated", success=True)
    await rail.after_tool_call(_ctx(agent, edit))

    failed = ToolCallInputs(tool_name="run_tests", tool_args={"files": ["parser.py"]},
                            tool_result=ToolOutput(success=True, data={"outcome": "failed", "passed_languages": []}))
    await rail.after_tool_call(_ctx(agent, failed))
    assert rail.runtime.state.completion_status().state == "verification_failed"

    passed = ToolCallInputs(tool_name="run_tests", tool_args={"files": ["parser.py"]},
                            tool_result=ToolOutput(success=True, data={"outcome": "passed", "passed_languages": ["python"]}))
    await rail.after_tool_call(_ctx(agent, passed))
    assert rail.runtime.state.completion_status().complete
    await rail.before_model_call(_ctx(agent, ModelCallInputs()))
    assert "implement_completeness" not in agent.system_prompt_builder.sections

    # no_tests carries no evidence: the passed verification stays in force
    no_tests = ToolCallInputs(tool_name="run_tests", tool_result=ToolOutput(success=True, data={"outcome": "no_tests", "passed_languages": []}))
    await rail.after_tool_call(_ctx(agent, no_tests))
    assert rail.runtime.state.completion_status().complete



@pytest.mark.asyncio
async def test_discipline_rail_treats_run_tests_as_verification():
    if CodeExecutionDisciplineRail is None:
        pytest.skip("coding-policy rail lands in prep stack B")
    builder = _Builder()
    agent = SimpleNamespace(system_prompt_builder=builder)
    rail = CodeExecutionDisciplineRail()
    rail.init(agent)
    rail._user_text = "Fix the parser"
    edit = ToolCallInputs(tool_name="edit_file", tool_args={"path": "a.py"}, tool_result=SimpleNamespace(content="ok", success=True))
    await rail.after_tool_call(_ctx(agent, edit))
    await rail.before_model_call(_ctx(agent, ModelCallInputs()))
    assert "run_tests" in builder.sections["code_execution_discipline"].content["en"]
    run = ToolCallInputs(tool_name="run_tests", tool_result=ToolOutput(success=True, data={"outcome": "passed", "passed_languages": ["python"]}))
    await rail.after_tool_call(_ctx(agent, run))
    await rail.before_model_call(_ctx(agent, ModelCallInputs()))
    assert not builder.sections


