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
