from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.expert.agent_group import load_agent_group_package
from jiuwenswarm.server.runtime.expert.expert_store import LocalDirExpertPackageSource
from jiuwenswarm.server.runtime.expert.team_materializer import (
    TeamMaterializationError,
    materialize_team_candidate,
)


def _expert(root: Path, expert_id: str, *, skill_name: str) -> Path:
    package = root / expert_id
    persona = package / "persona"
    skill = package / "skills" / skill_name
    persona.mkdir(parents=True)
    skill.mkdir(parents=True)
    (persona / "ROLE.md").write_text(f"# {expert_id}\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test\n---\n",
        encoding="utf-8",
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "packageType": "agent_template",
                "agentCard": {
                    "id": expert_id,
                    "name": expert_id,
                    "description": "test expert",
                },
                "persona": {"dir": "persona"},
                "skills": [{"dir": f"skills/{skill_name}", "mode": "all"}],
                "metadata": {"tags": ["test"]},
            }
        ),
        encoding="utf-8",
    )
    return package


def _candidate() -> dict:
    return {
        "id": "data-growth-team",
        "graphId": "graph-1",
        "name": "数据增长内容团",
        "description": "从数据洞察到内容成品",
        "memberIds": ["data-analyst", "content-designer"],
        "workflow": ["分析数据", "生成内容"],
        "quickPrompts": ["分析销售表并做一套推广方案"],
        "deliverables": ["可打开的 HTML 内容方案"],
    }


def test_materialize_team_preserves_member_skills(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }

    result = materialize_team_candidate(
        _candidate(), expert_packages=packages, destination_root=tmp_path / "experts"
    )

    templates = load_agent_group_package(result)
    assert list(templates) == ["leader", "data-analyst", "content-designer"]
    assert [Path(skill.dir).name for skill in templates["data-analyst"].skills] == [
        "excel-analysis"
    ]
    assert [Path(skill.dir).name for skill in templates["content-designer"].skills] == [
        "copywriter"
    ]
    assert templates["leader"].agent_card.id == "leader"
    top = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert top["metadata"]["memberExpertIds"] == [
        "data-analyst",
        "content-designer",
    ]
    assert "team-leader" in (result / "agents" / "leader" / "AGENT.md").read_text(
        encoding="utf-8"
    )


def test_materialize_renders_graph_workflow_steps(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }
    candidate = _candidate()
    candidate["workflow"] = [
        {
            "step": 1,
            "expertId": "data-analyst",
            "expertName": "数据分析师",
            "dependsOn": [],
        },
        {
            "step": 2,
            "expertId": "content-designer",
            "expertName": "内容设计师",
            "dependsOn": ["data-analyst"],
        },
    ]

    result = materialize_team_candidate(
        candidate, expert_packages=packages, destination_root=tmp_path / "experts"
    )

    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert "`data-analyst`（数据分析师）" in manifest["instruction"]
    assert "接收 data-analyst 的交接产物" in manifest["instruction"]
    assert manifest["metadata"]["workflow"][1].startswith("调度 `content-designer`")
    # Backward-compatible fallback: older candidates without handoff evidence
    # keep the human-readable dependency and do not invent a file contract.
    assert "交接输出：必须生成" not in manifest["instruction"]
    assert "mediaType=" not in manifest["instruction"]


def test_materialize_renders_hard_edge_handoff_contract(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }
    candidate = _candidate()
    candidate["workflow"] = [
        {
            "step": 1,
            "expertId": "data-analyst",
            "expertName": "数据分析师",
            "dependsOn": [],
            "handoffs": [],
        },
        {
            "step": 2,
            "expertId": "content-designer",
            "expertName": "内容设计师",
            "dependsOn": ["data-analyst"],
            "finalOutput": {
                "id": "campaign.html",
                "mediaType": "text/html",
                "schema": "xiaoyi.campaign.v1",
                "primary": True,
                "visibility": "public",
            },
            "handoffs": [
                {
                    "edgeId": "edge-1",
                    "fromExpertId": "data-analyst",
                    "evidence": [
                        {
                            "reason": "media type 与 schema 精确匹配",
                            "output": {
                                "id": "data-insight-brief.json",
                                "mediaType": "application/json",
                                "schema": "xiaoyi.data-insight.v1",
                            },
                            "input": {
                                "id": "data-insight-brief.json",
                                "mediaType": "application/json",
                                "schema": "xiaoyi.data-insight.v1",
                            },
                        }
                    ],
                }
            ],
        },
    ]

    result = materialize_team_candidate(
        candidate, expert_packages=packages, destination_root=tmp_path / "experts"
    )

    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    workflow = manifest["metadata"]["workflow"]
    assert "交接输出：必须生成 `.expert-handoffs/data-insight-brief.json`" in workflow[0]
    assert "mediaType=`application/json`" in workflow[0]
    assert "schema=`xiaoyi.data-insight.v1`" in workflow[0]
    assert "交接输入：必须读取 `.expert-handoffs/data-insight-brief.json`" in workflow[1]
    assert "不得改用 Markdown 等其他格式" in workflow[1]
    assert "最终主产物：必须生成 `campaign.html`" in workflow[1]
    assert "mediaType=`text/html`" in workflow[1]
    assert "schema=`xiaoyi.campaign.v1`" in workflow[1]
    assert "不得把大型完整正文塞入单次 `write_file`/`edit_file`" in workflow[1]
    assert "优先使用简短本地渲染脚本读取结构化交接并动态生成" in workflow[1]
    assert "无法采用时按有界小段分段写入" in workflow[1]
    assert "只向用户发送这个主文件" in workflow[1]
    assert ".expert-handoffs/data-insight-brief.json" in manifest["instruction"]
    assert "不得把大型完整正文塞入单次 `write_file`/`edit_file`" in manifest[
        "instruction"
    ]


@pytest.mark.parametrize(
    ("media_type", "expects_guidance"),
    [
        ("text/plain", True),
        ("Text/Markdown", True),
        ("application/pdf", False),
    ],
)
def test_materialize_adds_reliable_write_guidance_only_for_text_final_outputs(
    tmp_path: Path,
    media_type: str,
    expects_guidance: bool,
) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }
    candidate = _candidate()
    candidate["workflow"] = [
        {
            "step": 1,
            "expertId": "data-analyst",
            "expertName": "数据分析师",
            "dependsOn": [],
        },
        {
            "step": 2,
            "expertId": "content-designer",
            "expertName": "内容设计师",
            "dependsOn": ["data-analyst"],
            "finalOutput": {
                "id": "result.txt",
                "mediaType": media_type,
                "schema": "xiaoyi.result.v1",
                "primary": True,
                "visibility": "public",
            },
        },
    ]

    result = materialize_team_candidate(
        candidate, expert_packages=packages, destination_root=tmp_path / "experts"
    )

    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    workflow_clause = manifest["metadata"]["workflow"][1]
    guidance = "不得把大型完整正文塞入单次 `write_file`/`edit_file`"
    assert (guidance in workflow_clause) is expects_guidance
    assert (guidance in manifest["instruction"]) is expects_guidance


def test_materialize_accepts_agent_group_safe_non_slug_member_ids(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    packages = {
        "Data_分析师": _expert(sources, "Data_分析师", skill_name="excel-analysis"),
        "Content_Designer": _expert(
            sources, "Content_Designer", skill_name="copywriter"
        ),
    }
    candidate = _candidate()
    candidate["memberIds"] = ["Data_分析师", "Content_Designer"]

    result = materialize_team_candidate(
        candidate, expert_packages=packages, destination_root=tmp_path / "experts"
    )

    templates = load_agent_group_package(result)
    assert list(templates) == ["leader", "Data_分析师", "Content_Designer"]


def test_materialize_rejects_handoff_contract_that_would_be_truncated(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }
    candidate = _candidate()
    candidate["workflow"] = [
        {
            "expertId": "data-analyst",
            "expertName": "数据分析师",
            "dependsOn": [],
        },
        {
            "expertId": "content-designer",
            "expertName": "内容设计师",
            "dependsOn": ["data-analyst"],
            "handoffs": [
                {
                    "fromExpertId": "data-analyst",
                    "evidence": [
                        {
                            "output": {
                                "id": "brief.json",
                                "mediaType": "application/json",
                                "schema": "schema-" + "x" * 1600,
                            },
                            "input": {
                                "id": "brief.json",
                                "mediaType": "application/json",
                                "schema": "schema-" + "x" * 1600,
                            },
                        }
                    ],
                }
            ],
        },
    ]

    with pytest.raises(TeamMaterializationError, match="cannot be truncated"):
        materialize_team_candidate(
            candidate,
            expert_packages=packages,
            destination_root=tmp_path / "experts",
        )

    assert not (tmp_path / "experts" / candidate["id"]).exists()


@pytest.mark.asyncio
async def test_materialized_team_summary_retains_rich_metadata(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }
    destination_root = tmp_path / "experts"
    materialize_team_candidate(
        _candidate(), expert_packages=packages, destination_root=destination_root
    )

    summaries = await LocalDirExpertPackageSource(experts_dir=destination_root).list()

    assert len(summaries) == 1
    assert summaries[0].type == "team"
    assert summaries[0].tags == ["专家团", "智能协作", "图谱生成"]
    assert summaries[0].metadata["quickPrompts"] == ["分析销售表并做一套推广方案"]
    assert summaries[0].metadata["memberExpertIds"] == [
        "data-analyst",
        "content-designer",
    ]


def test_materialize_rejects_nested_team(tmp_path: Path) -> None:
    nested = tmp_path / "sources" / "data-analyst"
    nested.mkdir(parents=True)
    (nested / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "agent_group",
                "name": "data-analyst",
                "agents": ["leader"],
            }
        ),
        encoding="utf-8",
    )
    packages = {
        "data-analyst": nested,
        "content-designer": _expert(
            tmp_path / "sources", "content-designer", skill_name="copywriter"
        ),
    }

    with pytest.raises(TeamMaterializationError, match="not beta3-compatible|nested"):
        materialize_team_candidate(
            _candidate(),
            expert_packages=packages,
            destination_root=tmp_path / "experts",
        )


def test_materialize_never_overwrites_existing_package(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    packages = {
        "data-analyst": _expert(sources, "data-analyst", skill_name="excel-analysis"),
        "content-designer": _expert(
            sources, "content-designer", skill_name="copywriter"
        ),
    }
    destination_root = tmp_path / "experts"
    materialize_team_candidate(
        _candidate(), expert_packages=packages, destination_root=destination_root
    )

    with pytest.raises(TeamMaterializationError, match="already exists"):
        materialize_team_candidate(
            _candidate(), expert_packages=packages, destination_root=destination_root
        )


def test_materialize_rejects_unsafe_candidate_id(tmp_path: Path) -> None:
    candidate = _candidate()
    candidate["id"] = "../escape"
    with pytest.raises(TeamMaterializationError, match="safe package id"):
        materialize_team_candidate(
            candidate, expert_packages={}, destination_root=tmp_path / "experts"
        )
