# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agent keeps the real case of its project path; only cache keys fold case.

A desktop run on macOS showed the Code agent receiving
``/home-dir/projects/calc-project`` for a project at
``/Home-Dir/Projects/Calc-Project``.
Michael's turn-mutation store compares the model's (correctly cased) edit
paths against that workspace with a case-sensitive ``relative_to``, so no
edit was ever recorded, and the completion gate forced three extra model
calls and ended the turn with "no mutation recorded".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime import agent_manager as agent_manager_module
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_manager import (
    AgentManager,
    _make_agent_cache_key,
    _normalize_project_dir,
    _resolve_project_dir,
)
from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool


@pytest.fixture()
def mixed_case_project(tmp_path: Path) -> Path:
    project = tmp_path / "Mixed-Case" / "Calc-Project"
    (project / "calc").mkdir(parents=True)
    (project / "calc" / "__init__.py").write_text(
        "def mean(values):\n    return sum(values) / (len(values) + 1)\n"
    )
    (project / "test_calc.py").write_text(
        "from calc import mean\n\n\ndef test_mean():\n    assert mean([2, 4, 6]) == 4\n"
    )
    return project


@pytest.fixture()
def captured_configs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Run AgentManager for real but stop at the adapter boundary."""
    configs: list[dict[str, Any]] = []

    async def fake_create_instance(self, config=None, **_kwargs):
        configs.append(dict(config or {}))

    monkeypatch.setattr(
        interface_module.JiuWenSwarm, "create_instance", fake_create_instance
    )
    return configs


def test_resolve_keeps_case_and_key_folds_it(mixed_case_project: Path) -> None:
    upper = str(mixed_case_project)
    lower = upper.lower()
    assert _resolve_project_dir(upper) == upper
    assert _normalize_project_dir(upper) == _normalize_project_dir(lower)
    assert _make_agent_cache_key("code", "normal", upper) == _make_agent_cache_key(
        "code", "normal", lower
    )


async def test_agent_gets_real_case_and_case_variants_share_it(
    mixed_case_project: Path, captured_configs: list[dict[str, Any]]
) -> None:
    manager = AgentManager()
    first = await manager.get_agent(
        channel_id="web",
        mode="code",
        project_dir=str(mixed_case_project),
        sub_mode="normal",
    )
    assert first is not None
    assert captured_configs[-1]["project_dir"] == str(mixed_case_project)
    assert first._jiuwenswarm_agent_project_dir == str(mixed_case_project)

    # A different spelling of the same directory reuses the first agent.
    again = await manager.get_agent(
        channel_id="web",
        mode="code",
        project_dir=str(mixed_case_project).upper(),
        sub_mode="normal",
    )
    assert again is first
    assert len(captured_configs) == 1
    assert (
        manager.get_agent_nowait(
            channel_id="web", mode="code", project_dir=str(mixed_case_project).lower()
        )
        is first
    )


def test_warm_pool_key_ignores_case_but_carries_the_real_path(
    mixed_case_project: Path,
) -> None:
    upper = AgentWarmPool.make_key(
        channel_id="web",
        project_id="p",
        project_dir=str(mixed_case_project),
        work_mode="code",
    )
    lower = AgentWarmPool.make_key(
        channel_id="web",
        project_id="p",
        project_dir=str(mixed_case_project).lower(),
        work_mode="code",
    )
    assert upper == lower and hash(upper) == hash(lower)
    assert upper.project_path == str(mixed_case_project)


async def test_completion_gate_sees_the_edit_with_the_managed_project_dir(
    mixed_case_project: Path,
    captured_configs: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: AgentManager's project_dir -> adapter attribute -> Michael's rails."""
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.foundation.tool.schema import ToolOutput
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext,
        ModelCallInputs,
        ToolCallInputs,
        UserMessageInputs,
    )

    from jiuwenswarm.agents.harness.code.rails.code_completion_gate_rail import (
        CodeCompletionGateRail,
    )
    from jiuwenswarm.agents.harness.code.rails.implement_completeness_rail import (
        ImplementCompletenessRail,
    )

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    manager = AgentManager()
    await manager.get_agent(
        channel_id="web",
        mode="code",
        project_dir=str(mixed_case_project),
        sub_mode="normal",
    )
    project_dir = captured_configs[-1]["project_dir"]
    # interface_code.py sets these from config["project_dir"]
    agent = SimpleNamespace(
        _jiuwenswarm_code_project_dir=project_dir, _jiuwenswarm_project_dir=project_dir
    )

    def ctx(inputs: Any) -> AgentCallbackContext:
        return AgentCallbackContext(agent=agent, inputs=inputs)

    completeness, gate = ImplementCompletenessRail(), CodeCompletionGateRail()
    completeness.init(agent)
    gate.init(agent)
    await completeness.on_user_message(
        ctx(
            UserMessageInputs(
                parts=[
                    "the test fails. find the bug, fix it, and verify with the tests"
                ],
                source="query",
            )
        )
    )

    target = mixed_case_project / "calc" / "__init__.py"
    edit_args = json.dumps(
        {
            "file_path": str(target),
            "old_string": "(len(values) + 1)",
            "new_string": "len(values)",
        }
    )
    edit = ToolCallInputs(
        tool_call=ToolCall(
            id="call_edit", type="function", name="edit_file", arguments=edit_args
        ),
        tool_name="edit_file",
        tool_args=edit_args,
    )
    await completeness.before_tool_call(ctx(edit))
    target.write_text(target.read_text().replace("(len(values) + 1)", "len(values)"))
    edit.tool_result = ToolOutput(
        success=True, data={"file_path": str(target), "replacements": 1}
    )
    await completeness.after_tool_call(ctx(edit))

    command = "python3 -m pytest test_calc.py -q"
    run = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "pytest",
            "test_calc.py",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=mixed_case_project,
        capture_output=True,
        text=True,
        check=False,
    )
    bash_args = json.dumps({"command": command, "workdir": str(mixed_case_project)})
    bash = ToolCallInputs(
        tool_call=ToolCall(
            id="call_bash", type="function", name="bash", arguments=bash_args
        ),
        tool_name="bash",
        tool_args=bash_args,
        tool_result=ToolOutput(
            success=run.returncode == 0,
            data={
                "content": f"Command: {command}\nStdout: {run.stdout}\nExit Code: {run.returncode}"
            },
        ),
    )
    await completeness.before_tool_call(ctx(bash))
    await completeness.after_tool_call(ctx(bash))

    status = completeness.runtime.state.completion_status()
    assert status.complete, status.reason

    forced: list[str] = []
    final = ctx(
        ModelCallInputs(
            messages=[],
            response=SimpleNamespace(
                tool_calls=None, content="Fixed mean; tests pass."
            ),
        )
    )
    final.request_model_continue = lambda: forced.append("continue")
    final.request_force_finish = lambda reason: forced.append(str(reason))
    await gate.after_model_call(final)
    assert forced == []


def test_module_exports_are_stable() -> None:
    # Callers outside agent_manager use the key helper; keep it importable.
    assert callable(agent_manager_module._make_agent_cache_key)
