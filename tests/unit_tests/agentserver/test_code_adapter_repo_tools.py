# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the code adapter's repo-aware tool registration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from openjiuwen.core.foundation.tool import ToolExposure

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


def _patch_config(monkeypatch, config: dict) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.tool_ownership.Runner",
        SimpleNamespace(resource_mgr=_FakeResourceMgr()),
    )


def test_code_search_registered_from_tools_list(monkeypatch):
    _patch_config(monkeypatch, {"modes": {"code": {"tools": ["code_search"]}}})
    cards = JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id")
    assert [card.name for card in cards] == ["code_search"]
    card = cards[0]
    assert card.id == "code_search_agent-id"
    assert card.exposure == ToolExposure.DIRECT
    assert card.idempotent is True


def test_code_search_absent_when_not_listed(monkeypatch):
    _patch_config(monkeypatch, {"modes": {"code": {"tools": []}}})
    cards = JiuwenSwarmCodeAdapter().build_code_tool_cards("agent-id")
    assert "code_search" not in [card.name for card in cards]


def test_code_search_reads_repo_tools_config(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "modes": {
                "code": {
                    "tools": ["code_search"],
                    "repo_tools": {"code_search": {"max_results": 7, "context": 1}},
                }
            }
        },
    )
    adapter = JiuwenSwarmCodeAdapter()
    tool = adapter._build_code_search_tool("agent-id")
    assert tool._default_max_results == 7
    assert tool._default_context == 1


def test_root_resolver_follows_runtime_workspace(monkeypatch, tmp_path: Path):
    _patch_config(monkeypatch, {"modes": {"code": {"tools": []}}})
    adapter = JiuwenSwarmCodeAdapter()
    resolver = adapter._repo_tools_root_resolver()
    adapter._project_dir = str(tmp_path / "project")
    adapter._runtime_workspace_dir = None
    assert resolver() == str(tmp_path / "project")
    # A later workspace switch (worktree, project change) is seen without rebuilding.
    adapter._runtime_workspace_dir = str(tmp_path / "worktree")
    assert resolver() == str(tmp_path / "worktree")


def test_symbol_tools_registered_and_configured(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "modes": {
                "code": {
                    "tools": ["code_search", "find_symbol", "symbol_outline"],
                    "repo_tools": {
                        "symbols": {"max_results": 5, "max_outline_symbols": 9, "lsp_timeout": 2.5}
                    },
                }
            }
        },
    )
    adapter = JiuwenSwarmCodeAdapter()
    cards = adapter.build_code_tool_cards("agent-id")
    assert [card.name for card in cards] == ["code_search", "find_symbol", "symbol_outline"]
    assert all(card.exposure == ToolExposure.DIRECT for card in cards)
    find_tool = adapter._build_find_symbol_tool("agent-id")
    outline_tool = adapter._build_symbol_outline_tool("agent-id")
    assert find_tool._default_max_results == 5
    assert outline_tool._default_max_symbols == 9
    assert find_tool._lsp._timeout == 2.5


def test_repo_map_rail_default_on_and_configurable(monkeypatch):
    _patch_config(monkeypatch, {"modes": {"code": {"tools": []}}})
    rail = JiuwenSwarmCodeAdapter()._build_repo_map_rail()
    assert rail is not None and rail.depth == 2 and rail.max_chars == 6000
    _patch_config(
        monkeypatch,
        {"modes": {"code": {"tools": [], "repo_map": {"enabled": True, "depth": 3, "max_chars": 2500}}}},
    )
    rail = JiuwenSwarmCodeAdapter()._build_repo_map_rail()
    assert rail.depth == 3 and rail.max_chars == 2500


def test_repo_map_rail_disabled_by_config(monkeypatch):
    _patch_config(monkeypatch, {"modes": {"code": {"tools": [], "repo_map": {"enabled": False}}}})
    assert JiuwenSwarmCodeAdapter()._build_repo_map_rail() is None
    assert "RepoMapRail" in JiuwenSwarmCodeAdapter._FIXED_RAIL_NAMES


def test_ignored_dirs_config_reaches_tools_and_repo_map(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "modes": {
                "code": {
                    "tools": ["code_search", "find_symbol"],
                    "repo_tools": {"ignored_dirs": [".git", "generated"]},
                }
            }
        },
    )
    adapter = JiuwenSwarmCodeAdapter()
    cards = adapter.build_code_tool_cards("agent-id")
    from jiuwenswarm.common.tool_ownership import Runner

    search_tool = Runner.resource_mgr.get_tool("code_search_agent-id")
    find_tool = Runner.resource_mgr.get_tool("find_symbol_agent-id")
    assert [c.name for c in cards] == ["code_search", "find_symbol"]
    assert search_tool.engine.ignored_dirs == frozenset({".git", "generated"})
    assert find_tool._engine.ignored_dirs == frozenset({".git", "generated"})
    rail = adapter._build_repo_map_rail()
    assert rail.ignored_dirs == frozenset({".git", "generated"})


def test_ignored_dirs_default_when_unset(monkeypatch):
    from jiuwenswarm.agents.harness.code.repo_tools._common import IGNORED_DIRS

    _patch_config(monkeypatch, {"modes": {"code": {"tools": ["code_search"]}}})
    adapter = JiuwenSwarmCodeAdapter()
    adapter.build_code_tool_cards("agent-id")
    from jiuwenswarm.common.tool_ownership import Runner

    assert Runner.resource_mgr.get_tool("code_search_agent-id").engine.ignored_dirs == IGNORED_DIRS
    assert adapter._build_repo_map_rail().ignored_dirs == IGNORED_DIRS


def test_swarm_params_carry_ignored_dirs():
    from jiuwenswarm.agents.swarm.config_specs import (
        _repo_map_params,
        _repo_tools_params,
    )

    config = {
        "modes": {
            "code": {
                "tools": ["code_search"],
                "repo_tools": {"ignored_dirs": ["node_modules", "gen"]},
            }
        }
    }
    assert _repo_tools_params(config)["ignored_dirs"] == ["node_modules", "gen"]
    assert _repo_map_params(config)["ignored_dirs"] == ["node_modules", "gen"]
    assert _repo_tools_params({"modes": {"code": {"tools": ["code_search"]}}})["ignored_dirs"] is None
    assert _repo_map_params({})["ignored_dirs"] is None


def _secret_repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / "secret").mkdir(parents=True)
    (root / "secret" / "keys.py").write_text("API = 'TOPSECRET-123'\n", encoding="utf-8")
    (root / "app.py").write_text("x = 'TOPSECRET-app'\n", encoding="utf-8")
    return root


def _deny_secret_permissions() -> dict:
    import yaml

    template = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources" / "config.yaml"
    perms = dict(yaml.safe_load(template.read_text(encoding="utf-8"))["permissions"])
    perms["enabled"] = True
    fg = dict(perms["file_guard"])
    fg["paths"] = list(fg.get("paths") or []) + [
        {"path": "**/secret/**", "match": "glob", "read": "deny", "write": "deny", "exec": "deny"}
    ]
    perms["file_guard"] = fg
    return perms


def test_adapter_repo_tools_apply_session_read_policy(monkeypatch, tmp_path: Path):
    """The adapter wires the session's permissions into every repo tool and the map."""
    import asyncio

    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    monkeypatch.setattr(permissions_layers, "load_user_permissions", dict)
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: {})
    _patch_config(
        monkeypatch,
        {"modes": {"code": {"tools": ["code_search", "find_symbol", "symbol_outline"]}}},
    )
    root = _secret_repo(tmp_path)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(root)
    adapter._runtime_workspace_dir = None
    adapter._config_base_cache = {"permissions": _deny_secret_permissions()}

    search = adapter._build_code_search_tool("agent-id")
    out = asyncio.run(search.invoke({"pattern": "TOPSECRET"}))
    assert "secret/keys.py" not in out.data["content"]
    assert "are left out of these results" in out.data["content"]

    outline = asyncio.run(adapter._build_symbol_outline_tool("agent-id").invoke({"file_path": "secret/keys.py"}))
    assert not outline.success

    find = asyncio.run(adapter._build_find_symbol_tool("agent-id").invoke({"name": "API", "kind": "references"}))
    assert "secret/keys.py" not in find.data["content"]

    # Hot-reloaded permissions apply without rebuilding the tools.
    adapter._config_base_cache = {"permissions": {"enabled": False}}
    out = asyncio.run(search.invoke({"pattern": "TOPSECRET"}))
    assert "secret/keys.py" in out.data["content"]

    rail = adapter._build_repo_map_rail()
    assert rail._read_policy_factory is not None


def test_swarm_repo_tools_apply_member_read_policy(monkeypatch, tmp_path: Path):
    import asyncio

    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.code_rails import build_repo_map_rail
    from jiuwenswarm.agents.swarm.providers.tools import build_repo_tools

    monkeypatch.setattr(permissions_layers, "load_user_permissions", dict)
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: {})
    root = _secret_repo(tmp_path)
    ctx = SwarmBuildContext(
        session_id="s1", project_dir=str(root), config={"permissions": _deny_secret_permissions()}
    )
    tools = {t.card.name: t for t in build_repo_tools({}, ctx)}
    out = asyncio.run(tools["code_search"].invoke({"pattern": "TOPSECRET"}))
    assert "secret/keys.py" not in out.data["content"]
    assert not asyncio.run(tools["symbol_outline"].invoke({"file_path": "secret/keys.py"})).success
    assert build_repo_map_rail({}, ctx)._read_policy_factory is not None
