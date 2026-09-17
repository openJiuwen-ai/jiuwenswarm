# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.server.runtime.team_entity_store import (
    TeamEntityStore,
    ensure_team_entity_for_binding,
)
from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError


def test_team_entity_store_writes_team_workspace_metadata(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")

    entity = store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={"team_name": "template_team", "leader": {"member_name": "lead"}},
        created_at=123.0,
    )

    path = tmp_path / ".agent_teams" / "research_team" / "team-workspace" / ".team-meta" / "team.yaml"
    assert path.is_file()
    assert entity.team_name == "research_team"
    assert entity.created_at == 123.0

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["team_name"] == "research_team"
    assert raw["template_id"] == "default"
    assert raw["template_snapshot"]["leader"]["member_name"] == "lead"

    reloaded = store.get("research_team")
    assert reloaded is not None
    assert reloaded.template_snapshot["team_name"] == "template_team"


def test_team_entity_store_rejects_invalid_team_name(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")

    with pytest.raises(TeamBindingStoreError):
        store.write(
            team_name="../escape",
            template_id="default",
            template_snapshot={"team_name": "template_team"},
        )


def test_team_entity_store_delete_removes_complete_team_directory(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={"team_name": "template_team"},
    )
    team_path = tmp_path / ".agent_teams" / "research_team"
    artifact_path = team_path / "team-workspace" / "artifacts" / "report.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("report", encoding="utf-8")

    assert store.delete_team_directory("research_team") is True
    assert not team_path.exists()
    assert store.delete_team_directory("research_team") is False


def test_team_entity_store_delete_does_not_follow_team_symlink(tmp_path) -> None:
    teams_home = tmp_path / ".agent_teams"
    teams_home.mkdir()
    external_path = tmp_path / "external"
    external_path.mkdir()
    external_file = external_path / "keep.txt"
    external_file.write_text("keep", encoding="utf-8")
    team_link = teams_home / "research_team"
    team_link.symlink_to(external_path, target_is_directory=True)

    store = TeamEntityStore(teams_home)

    assert store.delete_team_directory("research_team") is True
    assert not team_link.exists()
    assert external_file.read_text(encoding="utf-8") == "keep"


def test_team_entity_store_delete_only_removes_metadata(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={"team_name": "template_team"},
    )
    team_path = tmp_path / ".agent_teams" / "research_team"
    artifact_path = team_path / "team-workspace" / "artifacts" / "report.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("report", encoding="utf-8")

    assert store.delete("research_team") is True
    assert not store.entity_path("research_team").exists()
    assert artifact_path.read_text(encoding="utf-8") == "report"


def test_ensure_team_entity_for_binding_migrates_from_current_config(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead"},
                }
            }
        }
    }

    entity = ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert entity is not None
    assert entity.team_name == "research_team"
    assert entity.template_id == "research"
    assert entity.template_snapshot["leader"]["member_name"] == "lead"
    assert store.entity_path("research_team").is_file()


def test_ensure_team_entity_for_binding_returns_none_when_template_missing(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="missing",
        created_at=42.0,
        template_snapshot=None,
    )

    assert ensure_team_entity_for_binding(binding, config_base={"modes": {"team": {}}}, store=store) is None
