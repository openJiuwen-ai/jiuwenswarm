# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.server.control import config_service


def _isolate_panel(monkeypatch, tmp_path, raw):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config_service, "get_env_file", lambda: env_file)
    monkeypatch.setattr(config_service, "_read_raw_config", raw if callable(raw) else lambda: raw)


def test_panel_returns_saved_design_generation_ids(monkeypatch, tmp_path):
    _isolate_panel(monkeypatch, tmp_path, {
        "design_generation": {
            "video_gen": " mm_video ",
            "visual_gen": "mm_image",
        },
    })
    payload = config_service.get_panel()
    assert payload["design_generation_video_gen"] == "mm_video"
    assert payload["design_generation_visual_gen"] == "mm_image"


def test_panel_design_generation_ids_are_empty_when_unset(monkeypatch, tmp_path):
    _isolate_panel(monkeypatch, tmp_path, {})
    payload = config_service.get_panel()
    assert payload["design_generation_video_gen"] == ""
    assert payload["design_generation_visual_gen"] == ""


def test_panel_design_generation_ids_default_when_yaml_merge_fails(monkeypatch, tmp_path):
    def broken():
        raise RuntimeError("yaml")

    _isolate_panel(monkeypatch, tmp_path, broken)
    payload = config_service.get_panel()
    assert payload["design_generation_video_gen"] == ""
    assert payload["design_generation_visual_gen"] == ""
