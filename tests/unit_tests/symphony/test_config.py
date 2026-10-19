import pytest

from jiuwenswarm.symphony import config as symphony_config


def test_symphony_config_defaults_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        symphony_config,
        "get_agent_workspace_dir",
        lambda: tmp_path / "agent" / "workspace",
    )

    cfg = symphony_config.symphony_config_from_dict({})

    assert cfg.paths.skills_root == (tmp_path / "agent" / "workspace" / "skills").resolve()
    assert cfg.paths.score_dir == (
        tmp_path / "agent" / "workspace" / "symphony" / "score"
    ).resolve()
    assert cfg.fingerprint.scan.max_depth is None
    assert cfg.fingerprint.extraction.body_limit is None
    assert cfg.build.batch_size == 12
    assert cfg.build.max_candidates_per_skill_relation == 32
    assert cfg.build.min_edge_confidence == 0.5
    assert cfg.orchestration.mode == "fast"
    assert cfg.orchestration.min_edge_confidence == 0.5
    assert cfg.evolution.enabled is False
    assert cfg.enabled is False


def test_symphony_config_normalizes_values(monkeypatch, tmp_path):
    monkeypatch.setattr(
        symphony_config,
        "get_agent_workspace_dir",
        lambda: tmp_path,
    )

    cfg = symphony_config.symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills-custom"),
                "score_dir": str(tmp_path / "score-custom"),
            },
            "fingerprint": {
                "scan": {
                    "max_depth": "6",
                },
                "extraction": {
                    "workers": 0,
                    "batch_size": "4",
                    "body_limit": "0",
                },
                "normalization": {
                    "workers": 2,
                    "batch_size": "5",
                    "duplicate_name_similarity_threshold": 2,
                    "max_vocab_size": "9",
                },
            },
            "build": {
                "workers": "3",
                "batch_size": "0",
                "max_candidates_per_skill_relation": "19",
                "require_consensus": "false",
                "min_edge_confidence": 2,
            },
            "orchestration": {
                "mode": "fast",
                "max_depth": "7",
                "min_edge_confidence": -1,
            },
            "evolution": {"enabled": "false"},
            "enabled": "true",
        }
    )

    assert cfg.paths.skills_root == (tmp_path / "skills-custom").resolve()
    assert cfg.paths.score_dir == (tmp_path / "score-custom").resolve()
    assert cfg.fingerprint.scan.max_depth == 6
    assert cfg.fingerprint.extraction.workers == 1
    assert cfg.fingerprint.extraction.batch_size == 4
    assert cfg.fingerprint.extraction.body_limit is None
    assert cfg.fingerprint.normalization.workers == 2
    assert cfg.fingerprint.normalization.batch_size == 5
    assert cfg.fingerprint.normalization.duplicate_name_similarity_threshold == 1.0
    assert cfg.fingerprint.normalization.max_vocab_size == 9
    assert cfg.build.workers == 3
    assert cfg.build.batch_size == 1
    assert cfg.build.max_candidates_per_skill_relation == 19
    assert cfg.build.require_consensus is False
    assert cfg.build.min_edge_confidence == 1.0
    assert cfg.orchestration.mode == "fast"
    assert cfg.orchestration.max_depth == 7
    assert cfg.orchestration.min_edge_confidence == 0.0
    assert cfg.evolution.enabled is False
    assert cfg.enabled is True


@pytest.mark.parametrize("mode", ["fast", "beam", "", None])
def test_symphony_config_accepts_supported_orchestration_modes(mode):
    cfg = symphony_config.symphony_config_from_dict(
        {"orchestration": {"mode": mode}}
    )

    assert cfg.orchestration.mode == (mode or "fast")


@pytest.mark.parametrize("mode", ["default", "graph", "unknown", "quick"])
def test_symphony_config_rejects_non_llm_orchestration_modes(mode):
    with pytest.raises(ValueError, match="Unsupported Symphony orchestration mode"):
        symphony_config.symphony_config_from_dict(
            {"orchestration": {"mode": mode}}
        )


def test_symphony_config_keeps_empty_max_vocab_size_as_none():
    cfg = symphony_config.symphony_config_from_dict(
        {"fingerprint": {"normalization": {"max_vocab_size": ""}}}
    )

    assert cfg.fingerprint.normalization.max_vocab_size is None


def test_symphony_config_keeps_empty_scan_and_body_limits_as_none():
    cfg = symphony_config.symphony_config_from_dict(
        {"fingerprint": {"scan": {"max_depth": ""}, "extraction": {"body_limit": ""}}}
    )

    assert cfg.fingerprint.scan.max_depth is None
    assert cfg.fingerprint.extraction.body_limit is None
