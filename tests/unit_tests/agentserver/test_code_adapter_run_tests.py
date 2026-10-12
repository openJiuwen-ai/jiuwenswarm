# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CodeAdapter and swarm registration of the run_tests tool."""

from types import SimpleNamespace

from jiuwenswarm.agents.harness.code.verification import RunTestsTool
from jiuwenswarm.agents.swarm import config_specs, registry
from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)


class _FakeResourceMgr:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}

    def get_tool(self, tool_id: str) -> object | None:
        return self._tools.get(tool_id)

    def add_tool(self, tool: object, **_kwargs) -> None:
        self._tools[tool.card.id] = tool


def _patch(monkeypatch, config):
    monkeypatch.setattr("jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config", lambda: config)
    monkeypatch.setattr("jiuwenswarm.common.tool_ownership.Runner", SimpleNamespace(resource_mgr=_FakeResourceMgr()))


def test_run_tests_registered_when_listed(monkeypatch, tmp_path):
    _patch(monkeypatch, {"modes": {"code": {"tools": ["run_tests"], "run_tests": {"timeout": 42, "max_summary_chars": 3000, "spill": False}}}})
    adapter = JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(tmp_path)
    adapter._runtime_workspace_dir = str(tmp_path)
    cards = adapter.build_code_tool_cards("agent-id")
    assert [c.name for c in cards] == ["run_tests"]
    tool = adapter._build_run_tests_tool("agent-id")
    assert isinstance(tool, RunTestsTool)
    assert tool._timeout == 42 and tool._max_summary_chars == 3000 and tool._spill is False
    assert str(tool._root()) == str(tmp_path.resolve())


def test_run_tests_absent_when_not_listed(monkeypatch):
    _patch(monkeypatch, {"modes": {"code": {"tools": []}}})
    assert JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id") == []
    assert interface_code._TOOL_BUILD_NAMES["run_tests"] == "_build_run_tests_tool"


def test_swarm_params_and_element():
    params = config_specs._run_tests_params({"modes": {"code": {"tools": ["run_tests"], "run_tests": {"timeout": 9}}}})
    assert params == {"enabled": True, "settings": {"timeout": 9}}
    assert config_specs._run_tests_params({"modes": {"code": {"tools": []}}})["enabled"] is False
    assert registry.RUN_TESTS in config_specs._CODE_TOOL_NAMES
    assert config_specs._tool_params(registry.RUN_TESTS, {"modes": {"code": {"tools": ["run_tests"]}}})["enabled"] is True

def test_prompt_guidance_only_when_tool_listed():
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
        RUN_TESTS_GUIDANCE,
        build_code_system_prompt,
    )

    base = build_code_system_prompt()
    assert "run_tests" not in base
    assert build_code_system_prompt(["bash"]) == base
    with_tool = build_code_system_prompt(["run_tests"])
    assert RUN_TESTS_GUIDANCE in with_tool
    assert len(RUN_TESTS_GUIDANCE) < 400  # ~75 tokens
