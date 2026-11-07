# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Model display must match dotenv's bare-key versus empty-value semantics."""

import os

import pytest
from dotenv import dotenv_values, load_dotenv

from jiuwenswarm.channels.process_cli import display_context


@pytest.mark.parametrize("line", ["PREVIEW_MODEL", "PREVIEW_MODEL=", "PREVIEW_MODEL=file-model", "# missing"])
@pytest.mark.parametrize("expression", ["${PREVIEW_MODEL}", "${PREVIEW_MODEL:-default-model}"])
def test_model_preview_matches_dotenv_loading(tmp_path, monkeypatch, line, expression):
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setenv("PREVIEW_MODEL", "shell-model")
    values = dotenv_values(env_file)
    entries = [{"model_client_config": {"model_name": expression}}]
    actual = display_context.select_configured_model_name(entries, dotenv=values)
    load_dotenv(env_file, override=True)
    expected = os.environ.get("PREVIEW_MODEL") or ("default-model" if ":-" in expression else None)
    assert actual == expected


def test_resolve_model_from_bare_dotenv_key_uses_shell(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n  defaults:\n    - model_client_config:\n        model_name: '${PREVIEW_MODEL:-default-model}'\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("PREVIEW_MODEL\n", encoding="utf-8")
    monkeypatch.setenv("PREVIEW_MODEL", "shell-model")
    monkeypatch.setattr(display_context, "_config_file_candidates", lambda: [config])
    assert display_context.resolve_configured_model_name() == "shell-model"
