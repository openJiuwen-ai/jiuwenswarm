# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
sys.path.insert(0, str(SCRIPT_DIR))

import eval_env  # noqa: E402
from eval_env import (  # noqa: E402
    contextbench_root_candidates,
    resolve_contextbench_parquet,
    resolve_contextbench_root,
)


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


def _fake_checkout(root: Path) -> Path:
    (root / "contextbench").mkdir(parents=True)
    (root / "contextbench" / "evaluate.py").write_text("# evaluate\n", encoding="utf-8")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "contextbench_verified.parquet").write_bytes(b"parquet")
    return root


def test_explicit_root_does_not_need_reconstruct_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_checkout(tmp_path / "cb")
    monkeypatch.delenv("CONTEXTBENCH_ROOT", raising=False)
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    found = resolve_contextbench_root(root)
    assert found == root.resolve()
    assert resolve_contextbench_parquet(root=found) == (
        root / "data" / "contextbench_verified.parquet"
    ).resolve()


def test_env_root_wins_over_missing_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    root = _fake_checkout(tmp_path / "elsewhere" / "ContextBench")
    monkeypatch.setattr(eval_env, "JIUWEN_ROOT", jiuwen)
    monkeypatch.setenv("CONTEXTBENCH_ROOT", str(root))
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    assert resolve_contextbench_root() == root.resolve()


def test_sibling_contextbench_is_the_portable_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    sibling = _fake_checkout(tmp_path / "ContextBench")
    monkeypatch.setattr(eval_env, "JIUWEN_ROOT", jiuwen)
    monkeypatch.delenv("CONTEXTBENCH_ROOT", raising=False)
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    assert resolve_contextbench_root() == sibling.resolve()


def test_missing_checkout_tells_testers_how_to_set_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    monkeypatch.setattr(eval_env, "JIUWEN_ROOT", jiuwen)
    monkeypatch.delenv("CONTEXTBENCH_ROOT", raising=False)
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    with pytest.raises(SystemExit) as exc:
        resolve_contextbench_root()
    message = str(exc.value)
    assert "CONTEXTBENCH_ROOT" in message
    assert "--contextbench-root" in message
    assert "../ContextBench" in message


def test_reconstruct_tmp_is_last_resort_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    monkeypatch.setattr(eval_env, "JIUWEN_ROOT", jiuwen)
    monkeypatch.delenv("CONTEXTBENCH_ROOT", raising=False)
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    looked = contextbench_root_candidates()
    assert str(looked[-1]).replace("\\", "/").endswith("reconstruct_tmp/ContextBench")
