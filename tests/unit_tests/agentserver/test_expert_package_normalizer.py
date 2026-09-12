from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.expert.expert_store import validate_expert_package
from jiuwenswarm.server.runtime.expert.package_normalizer import (
    ExpertNormalizationError,
    normalize_standalone_expert,
)


def _legacy_package(root: Path, expert_id: str = "research-prd-lead") -> Path:
    package = root / expert_id
    (package / "persona").mkdir(parents=True)
    (package / "skills" / "research-evidence-ledger").mkdir(parents=True)
    (package / "persona" / "ROLE.md").write_text("# role", encoding="utf-8")
    (package / "skills" / "research-evidence-ledger" / "SKILL.md").write_text(
        "---\nname: research-evidence-ledger\ndescription: test\n---\n",
        encoding="utf-8",
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "package_type": "agent_template",
                "name": expert_id,
                "description": "legacy",
                "persona": {"dir": "./persona"},
                "skills": [{"dir": "./skills/research-evidence-ledger", "mode": "all"}],
                "display_name": {"zh": "产品研究负责人", "en": "Research Lead"},
                "display_description": {"zh": "把调研材料变成PRD"},
                "category": "IndustryConsultant",
                "tags": [{"zh": "用户研究"}],
                "quick_inputs": [{"zh": "把这些调研材料整理成PRD"}],
            }
        ),
        encoding="utf-8",
    )
    return package


def test_normalize_legacy_expert_to_beta3(tmp_path: Path) -> None:
    source = _legacy_package(tmp_path / "source")
    collaboration = {
        "contractVersion": "1",
        "outputs": [
            {
                "id": "product-brief",
                "mediaType": "application/json",
                "schema": "xiaoyi.product-brief.v1",
            }
        ],
    }

    result = normalize_standalone_expert(
        source,
        destination_root=tmp_path / "destination",
        collaboration=collaboration,
    )

    assert validate_expert_package(result) == []
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["packageType"] == "agent_template"
    assert manifest["agentCard"]["name"] == "产品研究负责人"
    assert manifest["skills"][0]["dir"] == "skills/research-evidence-ledger"
    assert manifest["metadata"]["quickPrompts"] == ["把这些调研材料整理成PRD"]
    assert manifest["metadata"]["collaboration"] == collaboration


def test_normalize_never_overwrites_existing(tmp_path: Path) -> None:
    source = _legacy_package(tmp_path / "source")
    destination = tmp_path / "destination"
    normalize_standalone_expert(source, destination_root=destination)
    with pytest.raises(ExpertNormalizationError, match="already exists"):
        normalize_standalone_expert(source, destination_root=destination)


def test_normalize_rejects_group(tmp_path: Path) -> None:
    source = tmp_path / "source" / "team"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps({"package_type": "agent_group", "name": "team"}),
        encoding="utf-8",
    )
    with pytest.raises(ExpertNormalizationError, match="agent_group"):
        normalize_standalone_expert(source, destination_root=tmp_path / "destination")
