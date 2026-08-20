# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
sys.path.insert(0, str(SCRIPT_DIR))

import local_openjiuwen as eval_env  # noqa: E402


@pytest.fixture
def reset_eval_env(monkeypatch):
    monkeypatch.setattr(eval_env, "_LOADED_FROM", None)
    yield
    eval_env._LOADED_FROM = None


def test_project_dotenv_overrides_shell_api_key(tmp_path: Path, reset_eval_env, monkeypatch):
    monkeypatch.setenv("API_KEY", "sk-proj-from-zshrc")
    monkeypatch.setenv("API_BASE", "https://api.openai.com/v1")
    monkeypatch.setenv("MODEL_NAME", "gpt-4.1")
    env_file = tmp_path / ".env"
    env_file.write_text(
        'API_KEY="sk-or-from-project"\n'
        "API_BASE=https://example.invalid/v1\n"
        "MODEL_NAME=project-model\n"
        "MODEL_PROVIDER=OpenAI\n",
        encoding="utf-8",
    )
    loaded = eval_env.load_eval_dotenv(env_file, verbose=False)
    assert loaded == env_file.resolve()
    assert os.environ["API_KEY"] == "sk-or-from-project"
    assert os.environ["API_BASE"] == "https://example.invalid/v1"
    assert os.environ["MODEL_NAME"] == "project-model"
    masked = eval_env.mask_secret(os.environ["API_KEY"])
    assert masked.startswith("sk-or-")
    assert "sk-or-from-project" not in masked
