from pathlib import Path

import yaml


def test_default_team_config_enables_managed_worktrees():
    repo_root = Path(__file__).resolve().parents[2]
    config_file = repo_root / "jiuwenswarm" / "resources" / "config.yaml"

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    team_config = data["modes"]["team"]["jiuwen_team"]
    assert team_config["worktree"] == {"enabled": True}
    assert team_config["max_debate_rounds"] == 3


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


def test_default_telemetry_config_is_disabled_and_documents_unified_fields():
    repo_root = Path(__file__).resolve().parents[2]
    config_file = repo_root / "jiuwenswarm" / "resources" / "config.yaml"

    telemetry = yaml.safe_load(config_file.read_text(encoding="utf-8"))["telemetry"]

    assert telemetry["enabled"] is False
    assert telemetry["claw_id"] is None
    assert telemetry["redact_prompts"] is False
    assert telemetry["redact_completions"] is False
    assert telemetry["log_messages"] is True
    assert telemetry["session"] == {
        "stuck_threshold_ms": 300000,
        "stuck_check_interval_s": 30,
    }


def test_default_ttse_config_is_enabled():
    repo_root = Path(__file__).resolve().parents[2]
    config_file = repo_root / "jiuwenswarm" / "resources" / "config.yaml"

    react = yaml.safe_load(config_file.read_text(encoding="utf-8"))["react"]

    assert react["ttse"]["enabled"] is True
    assert react["evolution"]["enabled"] is True
    assert react["ttse"]["dream_interval"] == 50
    assert "trajectory_export_enabled" not in react["ttse"]
    assert "trajectory_export_path" not in react["ttse"]
    assert "ttse_consult" not in react["tool_lazy_load"]["eager_tools"]


def test_default_permissions_always_on_components_in_all_templates():
    """disabled 底线白名单必须写入全部模板：用户 override 的独有键会在
    deep-merge 后被丢弃（_load_permissions_from_yaml），漏一份即静默失效。"""
    repo_root = Path(__file__).resolve().parents[2]
    config_files = [
        repo_root / "jiuwenswarm" / "resources" / "config.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.leader.yaml",
        repo_root / "jiuwenswarm" / "resources" / "config.team.distributed.teammate.yaml",
    ]

    for config_file in config_files:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        permissions = data["permissions"]

        assert permissions["always_on_components"] == ["host_guard"]
