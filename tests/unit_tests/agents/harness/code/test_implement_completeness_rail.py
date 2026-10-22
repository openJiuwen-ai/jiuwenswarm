# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
    UserMessageInputs,
)

from jiuwenswarm.agents.harness.code.rails.implement_completeness_rail import (
    ImplementCompletenessRail,
)


class FakePromptBuilder:
    def __init__(self) -> None:
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def _ctx(agent, inputs, session_id="sess-code") -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=agent,
        inputs=inputs,
        session=SimpleNamespace(session_id=session_id),
    )


@pytest.mark.asyncio
async def test_completeness_uses_real_mutation_and_test_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.implement_completeness_rail.default_mutation_store_root",
        lambda session_id: tmp_path / "ledger" / session_id,
    )
    project = tmp_path / "project"
    project.mkdir()
    target = project / "parser.py"
    target.write_text("value = 1\n", encoding="utf-8")
    builder = FakePromptBuilder()
    agent = SimpleNamespace(
        system_prompt_builder=builder,
        _jiuwenswarm_code_project_dir=str(project),
    )
    rail = ImplementCompletenessRail()
    rail.init(agent)

    await rail.on_user_message(
        _ctx(agent, UserMessageInputs(parts=["Fix the parser"], source="query"))
    )
    edit_inputs = ToolCallInputs(
        tool_call=SimpleNamespace(id="edit-1"),
        tool_name="edit_file",
        tool_args={
            "path": str(target),
            "old_string": "value = 1",
            "new_string": "value = parse_value()",
        },
    )
    edit_ctx = _ctx(agent, edit_inputs)
    await rail.before_tool_call(edit_ctx)
    target.write_text("value = parse_value()\n", encoding="utf-8")
    edit_inputs.tool_result = SimpleNamespace(content="updated", success=True)
    await rail.after_tool_call(edit_ctx)

    await rail.before_model_call(_ctx(agent, ModelCallInputs()))
    assert "implement_completeness" in builder.sections
    assert rail.runtime is not None
    assert rail.runtime.state.completion_status().complete is False

    shell_inputs = ToolCallInputs(
        tool_name="bash",
        tool_args={"command": "python -m pytest tests/test_parser.py"},
        tool_result=SimpleNamespace(content="Exit Code: 0", success=True),
    )
    await rail.after_tool_call(_ctx(agent, shell_inputs))
    await rail.before_model_call(_ctx(agent, ModelCallInputs()))

    assert rail.runtime.state.completion_status().complete is True
    assert "implement_completeness" not in builder.sections

    await rail.after_invoke(_ctx(agent, {}))
    turns = rail.runtime.store.list_turns()
    assert len(turns) == 1
    assert turns[0].mutations[0].path == "parser.py"


@pytest.mark.asyncio
async def test_completeness_does_not_activate_for_explanation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.implement_completeness_rail.default_mutation_store_root",
        lambda session_id: tmp_path / "ledger" / session_id,
    )
    project = tmp_path / "project"
    project.mkdir()
    builder = FakePromptBuilder()
    agent = SimpleNamespace(
        system_prompt_builder=builder,
        _jiuwenswarm_code_project_dir=str(project),
    )
    rail = ImplementCompletenessRail()
    rail.init(agent)

    await rail.on_user_message(
        _ctx(agent, UserMessageInputs(parts=["Explain the parser"], source="query"))
    )
    await rail.before_model_call(_ctx(agent, ModelCallInputs()))

    assert "implement_completeness" not in builder.sections
