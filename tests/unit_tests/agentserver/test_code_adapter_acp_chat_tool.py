# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodeAdapter ACP tool registration."""

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter import interface_code
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter


class _FakeResourceMgr:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}

    def get_tool(self, tool_id: str) -> object | None:
        return self._tools.get(tool_id)

    def add_tool(self, tool: object) -> None:
        self._tools[tool.card.id] = tool


def test_code_adapter_builds_acp_chat_when_profile_configured(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        lambda: {
            "acp_agents": {"codex": {"command": "npx", "args": []}},
            "modes": {"code": {"tools": ["acp_chat"]}},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.Runner",
        SimpleNamespace(resource_mgr=_FakeResourceMgr()),
    )

    cards = JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id")

    assert [card.name for card in cards] == ["acp_chat"]


def test_code_adapter_skips_acp_chat_without_profiles(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        lambda: {
            "acp_agents": {},
            "modes": {"code": {"tools": ["acp_chat"]}},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.Runner",
        SimpleNamespace(resource_mgr=_FakeResourceMgr()),
    )

    cards = JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id")

    assert cards == []


def test_code_adapter_builds_coding_memory_rail_without_embedding_config(monkeypatch, tmp_path):
    created: dict[str, object] = {}

    class _FakeCodingMemoryRail:
        def __init__(self, *, coding_memory_dir, embedding_config, language):
            created["coding_memory_dir"] = coding_memory_dir
            created["embedding_config"] = embedding_config
            created["language"] = language

    monkeypatch.setattr(interface_code, "CodingMemoryRail", _FakeCodingMemoryRail)

    project_dir = tmp_path / "project"
    agent_workspace_dir = tmp_path / "agent_workspace"
    legacy_dir = project_dir / "coding_memory"
    legacy_dir.mkdir(parents=True)
    legacy_content = (
        "---\nname: Legacy\ndescription: Legacy memory\ntype: project\n---\n\nbody\n"
    )
    (legacy_dir / "legacy.md").write_text(legacy_content, encoding="utf-8")

    rail = interface_code.create_coding_memory_rail(
        project_dir=str(project_dir),
        agent_workspace_dir=str(agent_workspace_dir),
        config={"preferred_language": "zh", "embed": {}},
    )

    assert isinstance(rail, _FakeCodingMemoryRail)
    assert created["coding_memory_dir"] == str(
        tmp_path / "agent_workspace" / "coding_memory" / "project"
    )
    assert created["embedding_config"].model_name == "text-embedding-v3"
    assert created["embedding_config"].base_url == ""
    assert created["embedding_config"].api_key is None
    migrated = agent_workspace_dir / "coding_memory" / "project" / "legacy.md"
    assert migrated.read_text(encoding="utf-8") == legacy_content
