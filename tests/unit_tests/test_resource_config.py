from pathlib import Path

import yaml


def test_default_team_config_enables_managed_worktrees():
    repo_root = Path(__file__).resolve().parents[2]
    config_file = repo_root / "jiuwenswarm" / "resources" / "config.yaml"

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    assert data["modes"]["team"]["jiuwen_team"]["worktree"] == {"enabled": True}


def test_default_round_level_compressor_config_uses_context_ratio():
    repo_root = Path(__file__).resolve().parents[2]
    config_files = [
        repo_root / "jiuwenswarm" / "resources" / "config.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.leader.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.teammate.yaml",
    ]

    for config_file in config_files:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        round_level_config = data["react"]["context_engine_config"]["round_level_compressor_config"]

        assert round_level_config["trigger_context_ratio"] == 0.8
        assert "trigger_total_tokens" not in round_level_config
        assert "tokens_threshold" not in round_level_config


def test_distributed_team_configs_separate_heartbeat_jobs_and_health_check():
    repo_root = Path(__file__).resolve().parents[2]
    config_files = [
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.leader.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.teammate.yaml",
    ]

    for config_file in config_files:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert set(data["heartbeat"]) == {"jobs"}
        assert data["heartbeat"]["jobs"]["min_interval_seconds"] == 60
        assert data["heartbeat"]["jobs"]["execution_timeout_seconds"] == 300
        assert data["heartbeat"]["jobs"]["user_preemption_timeout_seconds"] == 10
        assert data["health_check"]["every"] == 3600
        assert data["health_check"]["target"] == "web"


def test_default_config_separates_heartbeat_jobs_and_health_check():
    repo_root = Path(__file__).resolve().parents[2]
    config_file = repo_root / "jiuwenswarm" / "resources" / "config.yaml"
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    assert set(data["heartbeat"]) == {"jobs"}
    assert data["heartbeat"]["jobs"]["min_interval_seconds"] == 60
    assert data["heartbeat"]["jobs"]["execution_timeout_seconds"] == 300
    assert data["heartbeat"]["jobs"]["user_preemption_timeout_seconds"] == 10
    assert data["health_check"]["every"] == 3600
    assert data["health_check"]["target"] == "web"


def test_default_skill_evolution_switch_is_disabled():
    repo_root = Path(__file__).resolve().parents[2]
    config_files = [
        repo_root / "jiuwenswarm" / "resources" / "config.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.leader.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.teammate.yaml",
    ]

    for config_file in config_files:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        evolution = data["react"]["evolution"]

        assert evolution["skill_evolution"] is False
        assert evolution["auto_save"] is False
        assert evolution["review_feedback_min_confidence"] == 0.7
