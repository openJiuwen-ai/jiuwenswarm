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


def _block_design_import(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "jiuwenswarm.server.runtime.designer.generation_selection":
            raise ImportError("design runtime unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)


def test_unrelated_setting_does_not_import_design_runtime(monkeypatch):
    from unittest.mock import Mock
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    update = Mock()
    monkeypatch.setattr(handlers, "update_rsi_enabled_in_config", update)
    _block_design_import(monkeypatch)
    result = handlers.apply_config_payload({"rsi_enabled": "true"})
    update.assert_called_once_with(True)
    assert "rsi_enabled" in result.yaml_updated


def test_wrong_cli_path_reports_validation_without_design_runtime(monkeypatch, tmp_path):
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    executable = tmp_path / "claude"
    executable.touch()
    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(handlers, "run_external_cli_version_command", lambda *_: ("2.1.206 (Claude Code)", ""))
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "")
    _block_design_import(monkeypatch)
    with pytest.raises(handlers.ConfigPanelBadRequest, match="does not identify.*codex"):
        handlers.apply_config_payload({
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "false",
            "external_cli_agent_codex_cli_path": str(executable),
        })


def test_design_import_failure_preserves_original_error(monkeypatch):
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    monkeypatch.setattr(handlers, "get_config_raw", lambda: {})
    _block_design_import(monkeypatch)
    with pytest.raises(ImportError, match="design runtime unavailable"):
        handlers.apply_config_payload({"design_generation_video_gen": "mm_test"})
