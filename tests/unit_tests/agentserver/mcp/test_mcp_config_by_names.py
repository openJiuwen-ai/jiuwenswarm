# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""resolve_connectors: expert connectors -> member MCP configs + bundled Skill dirs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.common import config as config_mod
from jiuwenswarm.common import mcp_config
from jiuwenswarm.server.runtime.mcp import state_store
from jiuwenswarm.server.runtime.skill.skilldev import state_utils


def _stdio_entry(name: str) -> dict:
    return {
        "name": name,
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", f"@demo/{name}"],
        "env": {"API_KEY": "${API_KEY}"},
        "enabled": False,  # web-connected connectors are stored disabled
    }


def _make_skills(root: Path, names: list[str]) -> Path:
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    (root / "README.md").write_text("not a skill", encoding="utf-8")
    return root


@pytest.fixture
def mcp_world(monkeypatch, tmp_path):
    entries = {"amap": _stdio_entry("amap"), "harmonyos-mcp": _stdio_entry("harmonyos-mcp")}
    states = {
        "amap": {"state": "connected"},
        "harmonyos-mcp": {"state": "connected"},
        "pending-mcp": {"state": "connecting"},
        "gitcode": {"state": "connected", "integration_type": "cli"},
    }
    gitcode_root = _make_skills(tmp_path / "mcp" / "skills" / "gitcode", ["gitcode-pr", "gitcode-issue"])
    skills_state = tmp_path / "skills_state.json"
    skills_state.write_text(json.dumps({"skill_configs": {}}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "get_mcp_servers", lambda: list(entries.values()))
    monkeypatch.setattr(state_store, "get_mcp_record", lambda name: states.get(name))
    monkeypatch.setattr(
        state_store,
        "connected_mcp_skill_dirs",
        lambda: [{"name": "gitcode", "dir": str(gitcode_root)}],
    )
    monkeypatch.setattr(state_utils, "get_state_file", lambda: skills_state)
    monkeypatch.setattr(
        mcp_config,
        "build_mcp_credential_resolver",
        lambda name: (lambda var: "real-secret" if var == "API_KEY" else None),
    )
    return {"states": states, "gitcode_root": gitcode_root, "skills_state": skills_state}


def test_connected_connector_builds_with_credentials_despite_enabled_false(mcp_world) -> None:
    configs, _skill_dirs = mcp_config.resolve_connectors(["amap"], server_id_scope="team:t:amap-expert")

    assert [cfg.server_name for cfg in configs] == ["amap"]
    assert "real-secret" in str(configs[0].params)
    assert "${API_KEY}" not in str(configs[0].params)


def test_unready_and_unknown_connectors_are_skipped(mcp_world) -> None:
    configs, skill_dirs = mcp_config.resolve_connectors(
        ["pending-mcp", "missing", "amap", "amap", ""],
        server_id_scope="team:t:m",
    )

    assert [cfg.server_name for cfg in configs] == ["amap"]
    assert skill_dirs == []


def test_connected_connectors_get_distinct_server_ids_within_one_member(mcp_world) -> None:
    """Two mounted servers on one member keep separate, per-member-stable ids."""
    first, _ = mcp_config.resolve_connectors(["amap", "harmonyos-mcp"], server_id_scope="team:t:m")
    again, _ = mcp_config.resolve_connectors(["harmonyos-mcp", "amap"], server_id_scope="team:t:m")

    assert [cfg.server_name for cfg in first] == ["amap", "harmonyos-mcp"]
    assert len({cfg.server_id for cfg in first}) == 2
    assert {cfg.server_id for cfg in again} == {cfg.server_id for cfg in first}


def test_server_id_is_scoped_per_member(mcp_world) -> None:
    a = mcp_config.resolve_connectors(["amap"], server_id_scope="team:t:expert-a")[0][0]
    b = mcp_config.resolve_connectors(["amap"], server_id_scope="team:t:expert-b")[0][0]
    again = mcp_config.resolve_connectors(["amap"], server_id_scope="team:t:expert-a")[0][0]

    assert a.server_id != b.server_id
    assert a.server_id == again.server_id


def test_non_cli_request_yields_no_skill_dirs(mcp_world) -> None:
    """Skill dirs come from the requested connectors only, never the whole workspace."""
    _configs, skill_dirs = mcp_config.resolve_connectors(["amap"], server_id_scope="team:t:m")

    assert skill_dirs == []


def test_cli_connector_yields_one_dir_per_skill(mcp_world) -> None:
    configs, skill_dirs = mcp_config.resolve_connectors(["gitcode", "amap"], server_id_scope="team:t:m")

    root = mcp_world["gitcode_root"]
    assert [cfg.server_name for cfg in configs] == ["amap"]
    assert skill_dirs == [str(root / "gitcode-issue"), str(root / "gitcode-pr")]


def test_disabled_connector_skills_are_left_out(mcp_world) -> None:
    _configs, skill_dirs = mcp_config.resolve_connectors(
        ["gitcode"], server_id_scope="team:t:m", disabled_skills={"gitcode-pr"}
    )

    assert skill_dirs == [str(mcp_world["gitcode_root"] / "gitcode-issue")]


def test_unconnected_cli_connector_contributes_nothing(mcp_world) -> None:
    mcp_world["states"]["gitcode"] = {"state": "disconnected", "integration_type": "cli"}

    assert mcp_config.resolve_connectors(["gitcode"], server_id_scope="team:t:m") == ([], [])


def test_unavailable_data_sources_degrade_to_empty(mcp_world, monkeypatch) -> None:
    """Bootstrap-time store failures skip the connectors instead of raising."""
    def broken():
        raise RuntimeError("store not ready")

    monkeypatch.setattr(config_mod, "get_mcp_servers", broken)
    monkeypatch.setattr(mcp_config, "_mcp_state", broken)

    assert mcp_config.resolve_connectors(["amap", "gitcode"], server_id_scope="team:t:m") == ([], [])


def test_registry_resolver_excludes_user_disabled_skills(mcp_world) -> None:
    """The swarm-side resolver feeds the user's disabled set into resolve_connectors."""
    from jiuwenswarm.agents.swarm import registry

    mcp_world["skills_state"].write_text(
        json.dumps({"skill_configs": {"gitcode-pr": {"enabled": False}}}),
        encoding="utf-8",
    )
    resolved = registry._resolve_template_connectors(["gitcode", "amap"], "t", "expert")

    assert [cfg.server_name for cfg in resolved.mcps] == ["amap"]
    assert resolved.skill_dirs == [str(mcp_world["gitcode_root"] / "gitcode-issue")]


def test_malformed_skill_dir_entries_are_skipped(mcp_world, monkeypatch) -> None:
    """A malformed skill-dir record never drops the other connectors' Skills."""
    root = mcp_world["gitcode_root"]
    monkeypatch.setattr(
        state_store,
        "connected_mcp_skill_dirs",
        lambda: [{"name": "gitcode", "dir": str(root)}, {"name": "broken"}, None, "junk"],
    )
    _configs, skill_dirs = mcp_config.resolve_connectors(["gitcode"], server_id_scope="team:t:m")

    assert skill_dirs == [str(root / "gitcode-issue"), str(root / "gitcode-pr")]


def test_registry_installs_resolver(mcp_world, monkeypatch) -> None:
    from openjiuwen.agent_teams.experts import template_connectors
    from jiuwenswarm.agents.swarm import registry

    # Snapshot the core global so monkeypatch restores it after the test —
    # register_swarm_providers installs the test-process resolver as a side
    # effect that plain monkeypatch cannot see.
    monkeypatch.setattr(template_connectors, "_resolver", template_connectors._resolver)
    monkeypatch.setattr(registry, "_REGISTERED", False)
    registry.register_swarm_providers()

    assert template_connectors._resolver is registry._resolve_template_connectors
