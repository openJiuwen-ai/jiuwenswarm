from __future__ import annotations

import json
from pathlib import Path

from scripts.prepare_expert_graph_demo import prepare_demo


def _source(root: Path, expert_id: str, skill_name: str) -> Path:
    package = root / expert_id
    (package / "persona").mkdir(parents=True)
    (package / "skills" / skill_name).mkdir(parents=True)
    (package / "persona" / "ROLE.md").write_text("# role\n", encoding="utf-8")
    (package / "skills" / skill_name / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: demo\n---\n",
        encoding="utf-8",
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "packageType": "agent_template",
                "agentCard": {
                    "id": expert_id,
                    "name": "测试专家",
                    "description": "测试描述",
                },
                "persona": {"dir": "persona"},
                "skills": [{"dir": f"skills/{skill_name}", "mode": "all"}],
            }
        ),
        encoding="utf-8",
    )
    return package


def test_prepare_demo_adds_contract_without_renaming_skill(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _source(source_root, "demo-expert", "original-skill-name")
    contracts = tmp_path / "contracts.json"
    contracts.write_text(
        json.dumps(
            {
                "demo-expert": {
                    "contractVersion": "xiaoyi.expert-collaboration.v1",
                    "outputs": [
                        {
                            "id": "handoff.json",
                            "mediaType": "application/json",
                            "schema": "demo.handoff.v1",
                            "visibility": "internal",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    result = prepare_demo(
        source_roots=[source_root],
        destination_root=tmp_path / "destination",
        contracts_path=contracts,
    )

    package = tmp_path / "destination" / "demo-expert"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert result["success"] is True
    assert manifest["skills"] == [{"dir": "skills/original-skill-name", "mode": "all"}]
    assert manifest["metadata"]["collaboration"]["outputs"][0]["schema"] == (
        "demo.handoff.v1"
    )
    persona = (package / "persona" / "99-collaboration-contract.md").read_text(
        encoding="utf-8"
    )
    assert ".expert-handoffs/" in persona
    assert "`team-leader`" in persona
