from __future__ import annotations

import json
from pathlib import Path

from scripts.flatten_expert_groups import flatten_expert_groups


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _skill(root: Path, directory: str, original_name: str) -> Path:
    skill = root / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {original_name}\ndescription: test skill\n---\n",
        encoding="utf-8",
    )
    return skill


def _team(
    root: Path,
    team_id: str,
    *,
    members: list[str],
    skills: list[str] | None = None,
) -> Path:
    team = root / team_id
    _write_json(
        team / "manifest.json",
        {
            "name": team_id,
            "package_type": "agent_group",
            "agents": ["leader", *members],
            "skills": skills or [],
            "instruction": "",
            "metadata": {
                "tags": ["测试团队"],
                "categoryId": "Test",
            },
        },
    )
    _write_json(
        team / "agents" / "leader" / "manifest.json",
        {"packageType": "agent_template", "persona": {"dir": "persona"}},
    )
    (team / "agents" / "leader" / "AGENT.md").write_text("# leader\n", encoding="utf-8")
    for member in members:
        _write_json(
            team / "agents" / member / "manifest.json",
            {
                "packageType": "agent_template",
                "name": f"{member}显示名",
                "description": f"{member}能力",
                "persona": {"dir": "persona"},
            },
        )
        persona = team / "agents" / member / "persona"
        persona.mkdir(parents=True)
        (persona / f"{member}.md").write_text("# member\n", encoding="utf-8")
    return team


def test_flattens_member_and_uses_original_skill_name(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    team = _team(sources, "ppt-team", members=["renderer"], skills=["legacy-dir"])
    source_skill = _skill(team / "skills", "legacy-dir", "original-skill-name")
    (source_skill / "settings.json").write_text(
        '{"apiKey":"must-not-copy"}', encoding="utf-8"
    )
    catalog = tmp_path / "catalog.json"
    _write_json(
        catalog,
        {
            "teams": {
                "ppt-team": {
                    "members": {
                        "renderer": {
                            "displayName": "PPT生成师",
                            "description": "生成可演示PPT",
                            "quickPrompt": "把这份材料做成一套PPT。",
                            "skills": ["original-skill-name"],
                            "collaboration": {
                                "outputs": [
                                    {
                                        "id": "presentation.html",
                                        "mediaType": "text/html",
                                        "schema": "xiaoyi.rendered-html.v1",
                                        "primary": True,
                                        "visibility": "public",
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        },
    )

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
        catalog_path=catalog,
    )

    package = tmp_path / "experts" / "ppt-team-renderer"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert result["summary"]["created"] == 1
    assert manifest["agentCard"]["id"] == "ppt-team-renderer"
    assert manifest["metadata"]["quickPrompts"] == ["把这份材料做成一套PPT。"]
    assert manifest["skills"] == [{"dir": "skills/original-skill-name", "mode": "all"}]
    assert (package / "skills" / "original-skill-name" / "SKILL.md").is_file()
    assert not (package / "skills" / "original-skill-name" / "settings.json").exists()
    assert result["created"][0]["filteredFiles"] == [
        "skills/original-skill-name/settings.json"
    ]


def test_catalog_can_mount_shared_skill_by_frontmatter_name(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    _team(sources, "data-team", members=["trend"])
    shared = tmp_path / "shared-skills"
    _skill(shared, "versioned-excel-skill-1.2.3", "excel-analysis")
    catalog = tmp_path / "catalog.json"
    _write_json(
        catalog,
        {
            "teams": {
                "data-team": {
                    "members": {
                        "trend": {
                            "skills": ["excel-analysis"],
                            "quickPrompt": "看看这份数据的趋势。",
                        }
                    }
                }
            }
        },
    )

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
        catalog_path=catalog,
        shared_skill_roots=[shared],
    )

    package = tmp_path / "experts" / "data-team-trend"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert result["summary"]["created"] == 1
    assert manifest["skills"][0]["dir"] == "skills/excel-analysis"
    assert (package / "skills" / "excel-analysis" / "SKILL.md").is_file()


def test_denies_academic_team_and_never_copies_it(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    team = _team(
        sources,
        "academic-journal-selector",
        members=["paper-reviewer"],
    )
    (team / "settings.json").write_text('{"credential":"secret"}', encoding="utf-8")

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
    )

    assert result["summary"]["created"] == 0
    assert result["skipped"] == [
        {
            "team": "academic-journal-selector",
            "member": "*",
            "reason": "denied-team",
        }
    ]
    assert not any((tmp_path / "experts").iterdir())


def test_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    _team(sources, "safe-team", members=["member"])
    existing = tmp_path / "experts" / "safe-team-member"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
    )

    assert result["summary"]["created"] == 0
    assert result["skipped"][0]["reason"] == "destination-exists"
    assert marker.read_text(encoding="utf-8") == "user data"


def test_member_persona_cannot_escape_package(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    team = _team(sources, "safe-team", members=["member"], skills=["safe-skill"])
    _skill(team / "skills", "safe-skill", "safe-skill")
    _write_json(
        team / "agents" / "member" / "manifest.json",
        {
            "packageType": "agent_template",
            "name": "成员",
            "persona": {"dir": "../../outside"},
        },
    )
    outside = sources / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("must not copy", encoding="utf-8")

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
    )

    assert result["summary"]["created"] == 0
    assert "unsafe member persona directory" in result["skipped"][0]["reason"]
    assert not any((tmp_path / "experts").iterdir())


def test_member_without_available_skills_is_skipped_by_default(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    _team(sources, "safe-team", members=["member"])

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
    )

    assert result["summary"]["created"] == 0
    assert result["skipped"][0]["reason"] == "no-available-skills"
    assert not any((tmp_path / "experts").iterdir())


def test_catalog_can_explicitly_allow_skillless_member(tmp_path: Path) -> None:
    sources = tmp_path / "groups"
    _team(sources, "safe-team", members=["member"])
    catalog = tmp_path / "catalog.json"
    _write_json(
        catalog,
        {"teams": {"safe-team": {"members": {"member": {"allowNoSkills": True}}}}},
    )

    result = flatten_expert_groups(
        source_roots=[sources],
        destination_root=tmp_path / "experts",
        catalog_path=catalog,
    )

    package = tmp_path / "experts" / "safe-team-member"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    collaboration = manifest["metadata"]["collaboration"]
    assert result["summary"]["created"] == 1
    assert collaboration == {
        "contractVersion": "xiaoyi.expert-collaboration.v1",
        "reusable": True,
    }
    assert len(manifest["metadata"]["quickPrompts"]) == 1
    assert "member显示名" in manifest["metadata"]["quickPrompts"][0]


def test_reviewed_catalog_covers_thirteen_high_value_roles() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    catalog = json.loads(
        (
            repository_root
            / "examples"
            / "xiaoyi_expert_graph_demo"
            / "expert_pool_catalog.json"
        ).read_text(encoding="utf-8")
    )

    teams = catalog["teams"]
    assert set(teams) == {
        "humanize-ppt-team",
        "huashu-data-pro",
        "software-company",
    }
    members = [member for team in teams.values() for member in team["members"].values()]
    assert len(members) == 13
    assert all(member["skills"] for member in members)
    assert all(len(member["quickPrompt"]) <= 40 for member in members)
    assert all(
        any(output.get("primary") for output in member["collaboration"]["outputs"])
        for member in members
    )
