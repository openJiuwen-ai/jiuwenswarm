# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ContextBench checkout resolution must not require a local reconstruct_tmp."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
sys.path.insert(0, str(SCRIPT_DIR))

import eval_paths  # noqa: E402
from eval_paths import (  # noqa: E402
    contextbench_root_candidates,
    resolve_contextbench_parquet,
    resolve_contextbench_root,
)


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
    monkeypatch.setattr(eval_paths, "JIUWEN_ROOT", jiuwen)
    monkeypatch.setenv("CONTEXTBENCH_ROOT", str(root))
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    assert resolve_contextbench_root() == root.resolve()


def test_sibling_contextbench_is_the_portable_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    sibling = _fake_checkout(tmp_path / "ContextBench")
    monkeypatch.setattr(eval_paths, "JIUWEN_ROOT", jiuwen)
    monkeypatch.delenv("CONTEXTBENCH_ROOT", raising=False)
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    assert resolve_contextbench_root() == sibling.resolve()


def test_missing_checkout_tells_testers_how_to_set_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jiuwen = tmp_path / "jiuwenswarm"
    jiuwen.mkdir()
    monkeypatch.setattr(eval_paths, "JIUWEN_ROOT", jiuwen)
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
    monkeypatch.setattr(eval_paths, "JIUWEN_ROOT", jiuwen)
    monkeypatch.delenv("CONTEXTBENCH_ROOT", raising=False)
    monkeypatch.delenv("CONTEXTBENCH_PARQUET", raising=False)
    looked = contextbench_root_candidates()
    assert str(looked[-1]).replace("\\", "/").endswith("reconstruct_tmp/ContextBench")
