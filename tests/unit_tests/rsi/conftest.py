"""Keep RSI installation tests out of the user's extension catalog."""

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime import extension_package_manager as catalog


@pytest.fixture
def rsi_catalog_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "agent-workspace"
    monkeypatch.setattr(catalog, "get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr(catalog, "get_equipment_resources_plugin_packages_dir", lambda: None)
    return workspace


@pytest.fixture
def rsi_harness_packages(tmp_path: Path) -> Path:
    """Provide self-contained Harness packages for RSI loading tests.

    Built-in plugin packages now live in the Hub; these packages deliberately
    exercise only the local materialization, catalog, and loading contracts.
    """

    root = tmp_path / "harness-packages"
    manifest_template = {
        "version": "1.0.0",
        "package_type": "plugin",
        "description": "Minimal RSI Harness test package.",
        "display_name": {"en": "RSI Harness", "zh": "RSI Harness"},
        "display_description": {"en": "Test Harness", "zh": "测试 Harness"},
        "tools": [{"file": "tools/test_tool.py", "class": "TestTool"}],
        "rails": [{"file": "rails/test_rail.py", "class": "TestRail"}],
        "skills": [{"dir": "skills/verification", "mode": "auto_list"}],
        "prompt_sections": [],
    }
    tool_source = '''from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard


class TestTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(id="rsi_test_tool", name="rsi_test_tool", description="RSI test tool"))

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def stream(self, inputs: dict[str, Any], **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)
'''
    rail_source = '''from openjiuwen.core.single_agent.rail.base import AgentRail


class TestRail(AgentRail):
    pass
'''
    skill = "---\nname: verification\ndescription: Verify changes before delivery.\n---\nRun relevant checks.\n"
    for name in ("coding-guard", "content-creation", "office-document-toolkit"):
        package = root / name
        manifest = {**manifest_template, "id": name, "name": name}
        (package / "tools").mkdir(parents=True)
        (package / "rails").mkdir()
        (package / "skills" / "verification").mkdir(parents=True)
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "README.md").write_text("# RSI Harness test package\n", encoding="utf-8")
        (package / "tools" / "test_tool.py").write_text(tool_source, encoding="utf-8")
        (package / "rails" / "test_rail.py").write_text(rail_source, encoding="utf-8")
        (package / "skills" / "verification" / "SKILL.md").write_text(skill, encoding="utf-8")
    return root
