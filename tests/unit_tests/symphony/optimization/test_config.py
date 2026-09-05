from jiuwenswarm.symphony.optimization import config as opt_config


def test_optimization_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(opt_config, "get_agent_workspace_dir", lambda: tmp_path)
    cfg = opt_config.optimization_config_from_dict({})

    assert cfg.enabled is False
    assert cfg.candidate_prompts == 5
    assert cfg.max_iterations == 6
    assert cfg.drift_penalty == 0.5
    assert cfg.min_correctness == 0.5
    assert cfg.reward_weights["correctness"] == 1.0
    assert cfg.resolved_memory_dir == (
        tmp_path / "symphony" / "optimization" / "prompt_kb"
    ).resolve()


def test_optimization_config_normalizes_and_merges_weights(monkeypatch, tmp_path):
    monkeypatch.setattr(opt_config, "get_agent_workspace_dir", lambda: tmp_path)
    cfg = opt_config.optimization_config_from_dict(
        {
            "enabled": "true",
            "candidate_prompts": 0,          # clamped up to 1
            "convergence_threshold": -5,     # clamped to >= 0
            "min_correctness": 2.0,          # clamped to <= 1
            "reward_weights": {"correctness": 3.0, "custom": 0.9, "bad": "x"},
            "models": {"judge_model": "  qwen-judge  "},
        }
    )

    assert cfg.enabled is True
    assert cfg.candidate_prompts == 1
    assert cfg.convergence_threshold == 0.0
    assert cfg.min_correctness == 1.0
    # explicit weights override, unspecified defaults survive, invalid dropped
    assert cfg.reward_weights["correctness"] == 3.0
    assert cfg.reward_weights["custom"] == 0.9
    assert cfg.reward_weights["completeness"] == 0.3
    assert "bad" not in cfg.reward_weights
    assert cfg.models.judge_model == "qwen-judge"


def test_load_optimization_config_reads_symphony_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(opt_config, "get_agent_workspace_dir", lambda: tmp_path)
    cfg = opt_config.load_optimization_config(
        {"symphony": {"optimization": {"enabled": True, "max_iterations": 9}}}
    )
    assert cfg.enabled is True
    assert cfg.max_iterations == 9
