import json
from types import SimpleNamespace

import pytest

from openjiuwen.symphony import (
    FINGERPRINT_ARTIFACT_FILENAME,
    FingerprintService,
    SkillFolderScanner,
)

from jiuwenswarm.symphony.adapter import (
    ScanResultCapabilityProvider,
    fingerprint_settings_from_swarm,
)
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.build import build_graph, graph_status
from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir
from jiuwenswarm.symphony.llm import LLMConfig


@pytest.mark.asyncio
async def test_core_fingerprint_service_builds_from_swarm_scan_adapter(tmp_path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "writer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Writer
description: Write a markdown document.
inputs:
  - name: topic
    type: string
outputs:
  - name: document
    type: markdown
---
Write a concise document for the supplied topic.
""",
        encoding="utf-8",
    )
    (skill_dir / "template.md").write_text("# {{ topic }}\n", encoding="utf-8")
    scan_result = SkillFolderScanner(skills_root).scan()
    config = symphony_config_from_dict(
        {
            "fingerprint": {
                "extraction": {
                    "workers": 2,
                    "batch_size": 3,
                }
            }
        }
    )
    artifact_root = tmp_path / "artifacts"

    artifact = await FingerprintService(
        ScanResultCapabilityProvider(scan_result),
        artifact_root,
        settings=fingerprint_settings_from_swarm(config, None),
    ).build()

    assert [item.capability_id for item in artifact.fingerprints] == ["writer"]
    assert (
        artifact.fingerprints[0].content_hash
        == scan_result.capabilities[0].content_hash
    )
    assert artifact.fingerprints[0].quality is not None
    artifact_path = artifact_root / FINGERPRINT_ARTIFACT_FILENAME
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["fingerprints"][0]["capability_id"] == "writer"
    assert "id" not in payload["fingerprints"][0]
    assert not (artifact_root / "fingerprints.json").exists()


def test_swarm_settings_map_only_supported_core_controls(tmp_path):
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(tmp_path / "graph"),
            },
            "fingerprint": {
                "extraction": {
                    "workers": 7,
                    "batch_size": 5,
                    "body_limit": 1234,
                }
            },
        }
    )

    settings = fingerprint_settings_from_swarm(config, None)

    assert settings.enable_llm_extraction is False
    assert settings.enable_llm_evaluation is False
    assert settings.max_concurrency == 7
    assert settings.batch_size == 5
    assert settings.body_limit == 1234


@pytest.mark.asyncio
async def test_swarm_graph_build_consumes_canonical_core_artifact(
    monkeypatch,
    tmp_path,
):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "summarizer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Summarizer
description: Summarize supplied text.
inputs:
  - name: source_text
    type: string
outputs:
  - name: summary
    type: string
---
Summarize the source faithfully.
""",
        encoding="utf-8",
    )
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(skills_root),
                "graph_dir": str(graph_dir),
            }
        }
    )
    model = _FingerprintAndGraphModel()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.adapter.model_from_config",
        lambda _config: model,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.model_from_config",
        lambda _config: model,
    )
    llm_config = LLMConfig(model="offline-test")

    first = await build_graph(
        skills_root,
        graph_dir,
        llm_config=llm_config,
        symphony_config=config,
    )
    second = await build_graph(
        skills_root,
        graph_dir,
        llm_config=llm_config,
        symphony_config=config,
    )

    version_dir = resolve_graph_artifact_dir(graph_dir)
    assert first.extracted_count == 1
    assert second.reused_count == 1
    assert (graph_dir / FINGERPRINT_ARTIFACT_FILENAME).is_file()
    assert (version_dir / FINGERPRINT_ARTIFACT_FILENAME).is_file()
    assert not (version_dir / "fingerprints.json").exists()
    assert (
        graph_status(
            skills_root,
            graph_dir,
            llm_config=llm_config,
            symphony_config=config,
        ).stale
        is False
    )


class _FingerprintAndGraphModel:
    async def invoke(self, messages, **kwargs):
        del kwargs
        system = str(messages[0].get("content") or "")
        if "Extract a capability fingerprint" in system:
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "description": "Summarize supplied text.",
                        "semantic_profile": {
                            "summary": "Summarize text.",
                            "capabilities": ["text summarization"],
                            "use_cases": ["shorten source text"],
                            "limitations": [],
                            "keywords": ["summary"],
                        },
                        "inputs": [{"name": "source_text", "type": "string"}],
                        "outputs": [{"name": "summary", "type": "string"}],
                        "classification": "writing",
                        "tags": ["summary"],
                    }
                )
            )
        return SimpleNamespace(content=json.dumps({"matches": []}))
