# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Extension SDK host service delegation."""

from pathlib import Path

from jiuwenswarm.extensions.sdk import host_config, workspace_paths


def test_config_services_use_host_functions(monkeypatch):
    from jiuwenswarm.common import config, reasoning_injector

    monkeypatch.setattr(config, "get_config", lambda: {"channels": {}})
    monkeypatch.setattr(config, "get_default_models", lambda settings: [settings])
    monkeypatch.setattr(
        reasoning_injector,
        "build_reasoning_model_request_kwargs",
        lambda **kwargs: kwargs,
    )

    assert host_config.get_config() == {"channels": {}}
    assert host_config.get_default_models({"models": {}}) == [{"models": {}}]
    monkeypatch.setattr(config, "get_default_models", lambda: [{"name": "default"}])
    assert host_config.get_default_models() == [{"name": "default"}]
    assert host_config.build_reasoning_model_request_kwargs(
        model_client_config={}, model_config_obj={}, model_name="demo"
    ) == {"model_client_config": {}, "model_config_obj": {}, "model_name": "demo"}


def test_workspace_paths_use_host_functions(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils

    for name in (
        "get_user_workspace_dir",
        "get_agent_workspace_dir",
        "get_agent_sessions_dir",
    ):
        monkeypatch.setattr(utils, name, lambda: Path(tmp_path))
        assert getattr(workspace_paths, name)() == tmp_path
