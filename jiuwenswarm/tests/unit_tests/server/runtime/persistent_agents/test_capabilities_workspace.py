from pathlib import Path

from jiuwenswarm.server.runtime.persistent_agents.capabilities import (
    default_capabilities,
    sync_workspace_equipment,
)


def test_default_capabilities_do_not_read_personal_expert_shelf() -> None:
    record = default_capabilities(
        "persistent_abc",
        "local",
        "persistent_template_abc",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert record.skill_names == []
    assert record.mcp_names == []


def test_sync_workspace_equipment_copies_skills_into_agent_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skills_root = tmp_path / "global_skills"
    (skills_root / "demo-skill").mkdir(parents=True)
    (skills_root / "demo-skill" / "SKILL.md").write_text("demo", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.persistent_agents.capabilities.get_agent_skills_dir",
        lambda: skills_root,
    )
    workspace = tmp_path / "agent" / "workspace"
    workspace.mkdir(parents=True)
    sync_workspace_equipment(workspace, skill_names=["demo-skill"])
    assert (workspace / "skills" / "demo-skill" / "SKILL.md").is_file()
