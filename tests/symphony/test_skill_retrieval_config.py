from __future__ import annotations

from pathlib import Path

from jiuwenswarm.symphony.skill_retrieval.config import load_settings
from jiuwenswarm.symphony.skill_retrieval.taxonomy_config import root_categories_to_text


def test_load_settings_reads_public_retrieve_config(monkeypatch) -> None:
    monkeypatch.delenv("SYMPHONY_SKILL_RETRIEVAL_ENABLED", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.config.get_agent_workspace_dir",
        lambda: Path("/tmp/jiuwenswarm-test-workspace"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.config.get_config",
        lambda: {
            "symphony": {
                "skill_retrieval": {
                    "enabled": True,
                    "build": {
                        "branching_factor": 64,
                        "root_categories": "taxonomy.yaml",
                        "max_depth": 12,
                        "request_timeout_seconds": 240,
                        "max_workers": 9,
                        "max_retries": 4,
                        "classification_batch_limit": 16,
                        "discovery_seed": 123,
                        "postprocess_enabled": False,
                        "postprocess_max_passes": 3,
                        "postprocess_min_skills": 9,
                        "equivalence_enabled": False,
                    },
                    "retrieve": {
                        "top_k": 7,
                        "compact_codes_enabled": True,
                        "flatten_tree": True,
                        "max_exposure_depth": 5,
                        "max_branch_choices": 8,
                    },
                }
            }
        },
    )

    settings = load_settings()

    assert settings.enabled is True
    assert settings.build.root_categories == "taxonomy.yaml"
    assert settings.build.branching_factor == 64
    assert settings.build.max_depth == 12
    assert settings.build.request_timeout_seconds == 240
    assert settings.build.max_workers == 9
    assert settings.build.max_retries == 4
    assert settings.build.classification_batch_limit == 16
    assert settings.build.discovery_seed == 123
    assert settings.build.postprocess_enabled is False
    assert settings.build.postprocess_max_passes == 3
    assert settings.build.postprocess_min_skills == 9
    assert settings.build.equivalence_enabled is False
    assert settings.retrieve.top_k == 7
    assert settings.retrieve.compact_codes_enabled is True
    assert settings.retrieve.flatten_tree is True
    assert settings.retrieve.max_exposure_depth == 5
    assert settings.retrieve.max_branch_choices == 2


def test_root_categories_panel_text_falls_back_from_unreadable_path() -> None:
    text = root_categories_to_text("missing-root-categories.yaml")

    assert text.startswith("tree_root_categories:")
    assert "external-service-automation" in text
