# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The config panel must report the models the runtime actually loads.

The generation slots are written by the runtime in another process: that process
updates its own ``os.environ`` and the saved ``.env``. The config query runs in a
long-lived process whose ``os.environ`` can still hold the values from before the
change, so the settings page kept showing the ModelArk defaults (doubao) while the
tools already ran on the newly selected MiniMax models.
"""

from jiuwenswarm.server.control import config_service


def test_panel_prefers_saved_env_over_process_env(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'VISUAL_GEN_MODEL_NAME="image-01"\n'
        'VISUAL_GEN_PROVIDER="MiniMax"\n'
        'VIDEO_GEN_MODEL_NAME="MiniMax-H3"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_service, "get_env_file", lambda: env_file)
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    # The long-lived process still holds the values from before the change.
    monkeypatch.setenv("VISUAL_GEN_MODEL_NAME", "doubao-seedream-5-0-flash-260915")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")

    payload = config_service.get_panel()

    assert payload["visual_gen_model"] == "image-01"
    assert payload["visual_gen_provider"] == "MiniMax"
    assert payload["video_gen_model"] == "MiniMax-H3"


def test_panel_falls_back_to_process_env_when_not_saved(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config_service, "get_env_file", lambda: env_file)
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    monkeypatch.setenv("VISUAL_GEN_MODEL_NAME", "process-only-model")

    assert config_service.get_panel()["visual_gen_model"] == "process-only-model"
